"""Read-only AWS adapter (boto3).

Safety: every client gets a `before-parameter-build` hook that rejects any
operation not in `READ_ONLY_OPERATIONS`, so even a programming error cannot
call a mutating API. The same list is documented as the IAM policy in
`docs/aws-readonly-policy.json` (a test keeps both in sync).

Nothing here reads object contents, EC2 user data, secrets or passwords.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from migrate.state import ResourceKind, SourceResource

# botocore service name -> operations this adapter may call.
READ_ONLY_OPERATIONS: dict[str, frozenset[str]] = {
    "sts": frozenset({"GetCallerIdentity"}),
    "ec2": frozenset(
        {
            "DescribeInstances",
            "DescribeInstanceTypes",
            "DescribeVolumes",
            "DescribeVpcs",
            "DescribeSubnets",
            "DescribeSecurityGroups",
        }
    ),
    "rds": frozenset({"DescribeDBInstances"}),
    "s3": frozenset(
        {
            "ListBuckets",
            "GetBucketLocation",
            "GetBucketTagging",
            "GetBucketVersioning",
            "GetBucketWebsite",
        }
    ),
    "cloudwatch": frozenset({"GetMetricData"}),
    "elbv2": frozenset(
        {
            "DescribeLoadBalancers",
            "DescribeListeners",
            "DescribeTargetGroups",
            "DescribeTargetHealth",
        }
    ),
    "eks": frozenset(
        {"ListClusters", "DescribeCluster", "ListNodegroups", "DescribeNodegroup"}
    ),
}

# IAM action prefix per botocore service name.
IAM_PREFIX = {"elbv2": "elasticloadbalancing"}
# Operations whose IAM action name differs from the API operation name.
IAM_ACTION_OVERRIDES = {("s3", "ListBuckets"): "s3:ListAllMyBuckets"}

# CloudWatch S3 storage metrics (daily). Archive tiers are flagged separately.
S3_STORAGE_TYPES = (
    "StandardStorage",
    "IntelligentTieringFAStorage",
    "IntelligentTieringIAStorage",
    "StandardIAStorage",
    "OneZoneIAStorage",
    "ReducedRedundancyStorage",
    "GlacierInstantRetrievalStorage",
    "GlacierStorage",
    "DeepArchiveStorage",
)
S3_ARCHIVE_TYPES = frozenset({"GlacierStorage", "DeepArchiveStorage"})

_ACTIVE_STATES = ["pending", "running", "stopping", "stopped"]


class ReadOnlyViolationError(PermissionError):
    """Raised before any request is sent if an operation is not read-only."""


def iam_actions() -> set[str]:
    actions = set()
    for service, operations in READ_ONLY_OPERATIONS.items():
        for op in operations:
            actions.add(
                IAM_ACTION_OVERRIDES.get(
                    (service, op), f"{IAM_PREFIX.get(service, service)}:{op}"
                )
            )
    return actions


def _guard(service: str):  # noqa: ANN202 - botocore handler
    allowed = READ_ONLY_OPERATIONS[service]

    def handler(model: Any, **_: Any) -> None:
        if model.name not in allowed:
            raise ReadOnlyViolationError(
                f"{service}:{model.name} is not on the read-only allowlist"
            )

    return handler


def _tags(raw: Iterable[dict[str, str]] | None) -> dict[str, str]:
    return {t["Key"]: t["Value"] for t in raw or [] if "Key" in t}


def _name(tags: dict[str, str], fallback: str) -> str:
    return tags.get("Name") or fallback


class AwsAdapter:
    """Implements `SourceCloudAdapter` for AWS. Read-only by construction."""

    cloud = "aws"

    def __init__(self, region: str, session: boto3.Session | None = None) -> None:
        self.region = region
        self._session = session or boto3.Session(region_name=region)
        self._clients: dict[str, Any] = {}
        self._type_cache: dict[str, dict[str, Any]] = {}
        self._instances: list[dict[str, Any]] | None = None

    def client(self, service: str) -> Any:
        if service not in READ_ONLY_OPERATIONS:
            raise ReadOnlyViolationError(f"service {service} is not allowed")
        if service not in self._clients:
            client = self._session.client(
                service,
                region_name=self.region,
                config=Config(
                    retries={"max_attempts": 8, "mode": "standard"},
                    user_agent_extra="stackit-migration-agents",
                ),
            )
            client.meta.events.register("before-parameter-build", _guard(service))
            self._clients[service] = client
        return self._clients[service]

    def _paginate(
        self, service: str, operation: str, key: str, **kwargs: Any
    ) -> Iterator[Any]:
        paginator = self.client(service).get_paginator(operation)
        for page in paginator.paginate(**kwargs):
            yield from page.get(key, [])

    # ------------------------------------------------------------ identity

    def account_id(self) -> str:
        return self.client("sts").get_caller_identity()["Account"]

    # ------------------------------------------------------------- compute

    def _raw_instances(self) -> list[dict[str, Any]]:
        if self._instances is None:
            self._instances = [
                inst
                for reservation in self._paginate(
                    "ec2",
                    "describe_instances",
                    "Reservations",
                    Filters=[{"Name": "instance-state-name", "Values": _ACTIVE_STATES}],
                )
                for inst in reservation.get("Instances", [])
            ]
        return self._instances

    def instance_type_specs(self, types: Iterable[str]) -> dict[str, dict[str, Any]]:
        types = list(types)  # iterated twice below
        missing = sorted({t for t in types if t and t not in self._type_cache})
        for start in range(0, len(missing), 100):
            for info in self._paginate(
                "ec2",
                "describe_instance_types",
                "InstanceTypes",
                InstanceTypes=missing[start : start + 100],
            ):
                self._type_cache[info["InstanceType"]] = {
                    "vcpus": info.get("VCpuInfo", {}).get("DefaultVCpus"),
                    "memory_mib": info.get("MemoryInfo", {}).get("SizeInMiB"),
                    "architectures": info.get("ProcessorInfo", {}).get(
                        "SupportedArchitectures", []
                    ),
                    "gpu": bool(info.get("GpuInfo")),
                }
        return {t: self._type_cache[t] for t in types if t in self._type_cache}

    def compute_instances(self) -> list[SourceResource]:
        raw = self._raw_instances()
        specs = self.instance_type_specs(i["InstanceType"] for i in raw)
        return [
            instance_to_resource(inst, specs.get(inst["InstanceType"], {}), self.region)
            for inst in raw
        ]

    def block_volumes(self) -> list[SourceResource]:
        roots = {
            (inst["InstanceId"], inst.get("RootDeviceName"))
            for inst in self._raw_instances()
            if inst.get("RootDeviceName")
        }
        return [
            volume_to_resource(vol, roots, self.region)
            for vol in self._paginate("ec2", "describe_volumes", "Volumes")
        ]

    # ------------------------------------------------------------ networks

    def networks(self) -> list[SourceResource]:
        resources = [
            vpc_to_resource(v, self.region)
            for v in self._paginate("ec2", "describe_vpcs", "Vpcs")
        ]
        resources += [
            subnet_to_resource(s, self.region)
            for s in self._paginate("ec2", "describe_subnets", "Subnets")
        ]
        return resources

    def security_groups(self) -> list[SourceResource]:
        return [
            security_group_to_resource(sg, self.region)
            for sg in self._paginate(
                "ec2", "describe_security_groups", "SecurityGroups"
            )
        ]

    # ----------------------------------------------------------- databases

    def databases(self) -> list[SourceResource]:
        raw = list(self._paginate("rds", "describe_db_instances", "DBInstances"))
        # RDS classes mirror EC2 types (db.m5.large ~ m5.large) for sizing.
        specs = self.instance_type_specs(
            db["DBInstanceClass"].removeprefix("db.")
            for db in raw
            if db.get("DBInstanceClass")
        )
        return [
            db_to_resource(
                db,
                specs.get(db.get("DBInstanceClass", "").removeprefix("db."), {}),
                self.region,
            )
            for db in raw
        ]

    # ------------------------------------------------------ object storage

    def object_buckets(self) -> list[SourceResource]:
        s3 = self.client("s3")
        resources = []
        for bucket in self._paginate("s3", "list_buckets", "Buckets"):
            name = bucket["Name"]
            try:
                location = s3.get_bucket_location(Bucket=name).get("LocationConstraint")
            except ClientError as exc:
                resources.append(_inaccessible_bucket(name, self.region, exc))
                continue
            bucket_region = {None: "us-east-1", "": "us-east-1", "EU": "eu-west-1"}.get(
                location, location
            )
            if bucket_region != self.region:
                continue
            props: dict[str, Any] = {
                "created": bucket.get("CreationDate"),
                **self._bucket_features(name),
            }
            props.update(self._bucket_metrics(name))
            tags = props.pop("_tags", {})
            resources.append(
                SourceResource(
                    id=f"s3:{name}",
                    name=name,
                    kind=ResourceKind.OBJECT_BUCKET,
                    source_type="aws:s3:bucket",
                    region=self.region,
                    tags=tags,
                    properties=props,
                )
            )
        return resources

    def _bucket_features(self, name: str) -> dict[str, Any]:
        s3 = self.client("s3")
        features: dict[str, Any] = {}
        try:
            features["_tags"] = _tags(s3.get_bucket_tagging(Bucket=name).get("TagSet"))
        except ClientError:
            features["_tags"] = {}
        try:
            features["versioning"] = s3.get_bucket_versioning(Bucket=name).get(
                "Status", "Disabled"
            )
        except ClientError:
            features["versioning"] = "unknown"
        try:
            s3.get_bucket_website(Bucket=name)
            features["website"] = True
        except ClientError as exc:
            features["website"] = (
                False if _code(exc) == "NoSuchWebsiteConfiguration" else "unknown"
            )
        return features

    def _bucket_metrics(self, name: str) -> dict[str, Any]:
        """Size and object count from CloudWatch daily storage metrics."""
        end = datetime.now(UTC)
        queries = [
            {
                "Id": f"s{i}",
                "MetricStat": {
                    "Metric": {
                        "Namespace": "AWS/S3",
                        "MetricName": "BucketSizeBytes",
                        "Dimensions": [
                            {"Name": "BucketName", "Value": name},
                            {"Name": "StorageType", "Value": storage_type},
                        ],
                    },
                    "Period": 86400,
                    "Stat": "Average",
                },
            }
            for i, storage_type in enumerate(S3_STORAGE_TYPES)
        ]
        queries.append(
            {
                "Id": "objects",
                "MetricStat": {
                    "Metric": {
                        "Namespace": "AWS/S3",
                        "MetricName": "NumberOfObjects",
                        "Dimensions": [
                            {"Name": "BucketName", "Value": name},
                            {"Name": "StorageType", "Value": "AllStorageTypes"},
                        ],
                    },
                    "Period": 86400,
                    "Stat": "Average",
                },
            }
        )
        latest: dict[str, float] = {}
        for result in self._paginate(
            "cloudwatch",
            "get_metric_data",
            "MetricDataResults",
            MetricDataQueries=queries,
            StartTime=end - timedelta(days=3),
            EndTime=end,
            ScanBy="TimestampDescending",
        ):
            if result.get("Values"):
                latest[result["Id"]] = float(result["Values"][0])
        by_type = {
            storage_type: int(latest[f"s{i}"])
            for i, storage_type in enumerate(S3_STORAGE_TYPES)
            if f"s{i}" in latest
        }
        return {
            "size_bytes": sum(by_type.values()),
            "object_count": int(latest.get("objects", 0)),
            "storage_classes": by_type,
            "has_archive_storage": any(t in S3_ARCHIVE_TYPES for t in by_type),
            "metrics_source": "cloudwatch-daily",
        }

    # ------------------------------------------------------ load balancers

    def load_balancers(self) -> list[SourceResource]:
        elb = self.client("elbv2")
        resources = []
        for lb in self._paginate("elbv2", "describe_load_balancers", "LoadBalancers"):
            arn = lb["LoadBalancerArn"]
            listeners = list(
                self._paginate(
                    "elbv2", "describe_listeners", "Listeners", LoadBalancerArn=arn
                )
            )
            groups = list(
                self._paginate(
                    "elbv2",
                    "describe_target_groups",
                    "TargetGroups",
                    LoadBalancerArn=arn,
                )
            )
            health = {
                g["TargetGroupArn"]: elb.describe_target_health(
                    TargetGroupArn=g["TargetGroupArn"]
                ).get("TargetHealthDescriptions", [])
                for g in groups
            }
            resources.append(
                load_balancer_to_resource(lb, listeners, groups, health, self.region)
            )
        return resources

    # ----------------------------------------------------------- kubernetes

    def kubernetes_clusters(self) -> list[SourceResource]:
        eks = self.client("eks")
        resources = []
        for name in self._paginate("eks", "list_clusters", "clusters"):
            cluster = eks.describe_cluster(name=name)["cluster"]
            nodegroups = [
                eks.describe_nodegroup(clusterName=name, nodegroupName=ng)["nodegroup"]
                for ng in self._paginate(
                    "eks", "list_nodegroups", "nodegroups", clusterName=name
                )
            ]
            specs = self.instance_type_specs(
                t for ng in nodegroups for t in ng.get("instanceTypes") or []
            )
            resources.append(eks_to_resource(cluster, nodegroups, specs, self.region))
        return resources


# --------------------------------------------------------------------------
# Normalisation (pure functions, unit tested with recorded API responses)
# --------------------------------------------------------------------------


def _code(exc: ClientError) -> str:
    return exc.response.get("Error", {}).get("Code", "")


def _inaccessible_bucket(name: str, region: str, exc: ClientError) -> SourceResource:
    return SourceResource(
        id=f"s3:{name}",
        name=name,
        kind=ResourceKind.OBJECT_BUCKET,
        source_type="aws:s3:bucket",
        region=region,
        properties={"access_error": _code(exc) or str(exc)},
    )


def instance_to_resource(
    inst: dict[str, Any], spec: dict[str, Any], region: str
) -> SourceResource:
    tags = _tags(inst.get("Tags"))
    sg_ids = [g["GroupId"] for g in inst.get("SecurityGroups", [])]
    volumes = [
        {"device": m["DeviceName"], "volume_id": m["Ebs"]["VolumeId"]}
        for m in inst.get("BlockDeviceMappings", [])
        if "Ebs" in m
    ]
    eks_cluster = tags.get("eks:cluster-name") or tags.get("aws:eks:cluster-name")
    return SourceResource(
        id=inst["InstanceId"],
        name=_name(tags, inst["InstanceId"]),
        kind=ResourceKind.COMPUTE_INSTANCE,
        source_type="aws:ec2:instance",
        region=region,
        tags=tags,
        properties={
            "instance_type": inst["InstanceType"],
            "vcpus": spec.get("vcpus"),
            "memory_mib": spec.get("memory_mib"),
            "gpu": spec.get("gpu", False),
            "architecture": inst.get("Architecture", "x86_64"),
            "platform": "windows" if inst.get("Platform") == "windows" else "linux",
            "platform_details": inst.get("PlatformDetails"),
            "state": inst.get("State", {}).get("Name"),
            "availability_zone": inst.get("Placement", {}).get("AvailabilityZone"),
            "private_ip": inst.get("PrivateIpAddress"),
            "public_ip": inst.get("PublicIpAddress"),
            "vpc_id": inst.get("VpcId"),
            "subnet_id": inst.get("SubnetId"),
            "security_group_ids": sg_ids,
            "image_id": inst.get("ImageId"),
            "root_device": inst.get("RootDeviceName"),
            "volumes": volumes,
            "eks_cluster": eks_cluster,
        },
        depends_on=[x for x in [inst.get("SubnetId"), *sg_ids] if x],
    )


def volume_to_resource(
    vol: dict[str, Any], roots: set[tuple[str, str]], region: str
) -> SourceResource:
    tags = _tags(vol.get("Tags"))
    attachments = [
        {"instance_id": a["InstanceId"], "device": a["Device"]}
        for a in vol.get("Attachments", [])
    ]
    is_root = any((a["instance_id"], a["device"]) in roots for a in attachments)
    return SourceResource(
        id=vol["VolumeId"],
        name=_name(tags, vol["VolumeId"]),
        kind=ResourceKind.BLOCK_VOLUME,
        source_type="aws:ec2:volume",
        region=region,
        tags=tags,
        properties={
            "size_gib": vol["Size"],
            "volume_type": vol.get("VolumeType"),
            "iops": vol.get("Iops"),
            "throughput": vol.get("Throughput"),
            "encrypted": vol.get("Encrypted", False),
            "availability_zone": vol.get("AvailabilityZone"),
            "attachments": attachments,
            "is_root": is_root,
            "state": vol.get("State"),
        },
        depends_on=[a["instance_id"] for a in attachments],
    )


def vpc_to_resource(vpc: dict[str, Any], region: str) -> SourceResource:
    tags = _tags(vpc.get("Tags"))
    return SourceResource(
        id=vpc["VpcId"],
        name=_name(tags, vpc["VpcId"]),
        kind=ResourceKind.NETWORK,
        source_type="aws:ec2:vpc",
        region=region,
        tags=tags,
        properties={
            "cidr_block": vpc.get("CidrBlock"),
            "is_default": vpc.get("IsDefault", False),
        },
    )


def subnet_to_resource(subnet: dict[str, Any], region: str) -> SourceResource:
    tags = _tags(subnet.get("Tags"))
    return SourceResource(
        id=subnet["SubnetId"],
        name=_name(tags, subnet["SubnetId"]),
        kind=ResourceKind.SUBNET,
        source_type="aws:ec2:subnet",
        region=region,
        tags=tags,
        properties={
            "cidr_block": subnet.get("CidrBlock"),
            "availability_zone": subnet.get("AvailabilityZone"),
            "vpc_id": subnet.get("VpcId"),
            "public": subnet.get("MapPublicIpOnLaunch", False),
        },
        depends_on=[subnet["VpcId"]] if subnet.get("VpcId") else [],
    )


def _rules(permissions: list[dict[str, Any]], direction: str) -> list[dict[str, Any]]:
    rules: list[dict[str, Any]] = []
    for perm in permissions:
        protocol = perm.get("IpProtocol", "-1")
        base = {
            "direction": direction,
            "protocol": "all" if protocol == "-1" else protocol,
            "port_min": perm.get("FromPort"),
            "port_max": perm.get("ToPort"),
        }
        for r in perm.get("IpRanges", []):
            rules.append(
                {**base, "cidr": r["CidrIp"], "description": r.get("Description", "")}
            )
        for r in perm.get("Ipv6Ranges", []):
            rules.append(
                {**base, "cidr": r["CidrIpv6"], "description": r.get("Description", "")}
            )
        for g in perm.get("UserIdGroupPairs", []):
            rules.append(
                {
                    **base,
                    "source_security_group": g["GroupId"],
                    "description": g.get("Description", ""),
                }
            )
        for p in perm.get("PrefixListIds", []):
            rules.append(
                {
                    **base,
                    "prefix_list": p["PrefixListId"],
                    "description": p.get("Description", ""),
                }
            )
    return rules


def security_group_to_resource(sg: dict[str, Any], region: str) -> SourceResource:
    tags = _tags(sg.get("Tags"))
    rules = _rules(sg.get("IpPermissions", []), "ingress") + _rules(
        sg.get("IpPermissionsEgress", []), "egress"
    )
    refs = sorted(
        {r["source_security_group"] for r in rules if r.get("source_security_group")}
        - {sg["GroupId"]}
    )
    return SourceResource(
        id=sg["GroupId"],
        name=sg.get("GroupName") or _name(tags, sg["GroupId"]),
        kind=ResourceKind.SECURITY_GROUP,
        source_type="aws:ec2:security-group",
        region=region,
        tags=tags,
        properties={
            "description": sg.get("Description", ""),
            "vpc_id": sg.get("VpcId"),
            "rules": rules,
        },
        depends_on=refs,
    )


_ENGINE_KIND = {
    "postgres": ResourceKind.POSTGRES,
    "aurora-postgresql": ResourceKind.POSTGRES,
    "mysql": ResourceKind.MYSQL,
    "aurora-mysql": ResourceKind.MYSQL,
    "aurora": ResourceKind.MYSQL,
    "mariadb": ResourceKind.MARIADB,
}


def db_to_resource(
    db: dict[str, Any], spec: dict[str, Any], region: str
) -> SourceResource:
    engine = db.get("Engine", "")
    endpoint = db.get("Endpoint") or {}
    subnet_ids = [
        s["SubnetIdentifier"]
        for s in (db.get("DBSubnetGroup") or {}).get("Subnets", [])
    ]
    sg_ids = [g["VpcSecurityGroupId"] for g in db.get("VpcSecurityGroups", [])]
    return SourceResource(
        id=f"rds:{db['DBInstanceIdentifier']}",
        name=db["DBInstanceIdentifier"],
        kind=_ENGINE_KIND.get(engine, ResourceKind.OTHER),
        source_type=f"aws:rds:{engine or 'unknown'}",
        region=region,
        tags=_tags(db.get("TagList")),
        properties={
            "engine": engine,
            "engine_version": db.get("EngineVersion"),
            "instance_class": db.get("DBInstanceClass"),
            "vcpus": spec.get("vcpus"),
            "memory_mib": spec.get("memory_mib"),
            "allocated_storage_gb": db.get("AllocatedStorage"),
            "storage_type": db.get("StorageType"),
            "iops": db.get("Iops"),
            "multi_az": db.get("MultiAZ", False),
            "publicly_accessible": db.get("PubliclyAccessible", False),
            "endpoint": {
                "address": endpoint.get("Address"),
                "port": endpoint.get("Port"),
            },
            "db_name": db.get("DBName"),
            "backup_retention_days": db.get("BackupRetentionPeriod"),
            "storage_encrypted": db.get("StorageEncrypted", False),
            "availability_zone": db.get("AvailabilityZone"),
            "vpc_security_group_ids": sg_ids,
            "subnet_ids": subnet_ids,
        },
        depends_on=sg_ids,
    )


def load_balancer_to_resource(
    lb: dict[str, Any],
    listeners: list[dict[str, Any]],
    groups: list[dict[str, Any]],
    health: dict[str, list[dict[str, Any]]],
    region: str,
) -> SourceResource:
    targets = sorted(
        {
            d["Target"]["Id"]
            for descs in health.values()
            for d in descs
            if d.get("Target", {}).get("Id")
        }
    )
    return SourceResource(
        id=f"elb:{lb['LoadBalancerName']}",
        name=lb["LoadBalancerName"],
        kind=ResourceKind.LOAD_BALANCER,
        source_type=f"aws:elbv2:{lb.get('Type', 'unknown')}",
        region=region,
        properties={
            "lb_type": lb.get("Type"),
            "scheme": lb.get("Scheme"),
            "vpc_id": lb.get("VpcId"),
            "subnet_ids": [
                az["SubnetId"]
                for az in lb.get("AvailabilityZones", [])
                if az.get("SubnetId")
            ],
            "security_group_ids": lb.get("SecurityGroups", []),
            "listeners": [
                {
                    "protocol": li.get("Protocol"),
                    "port": li.get("Port"),
                    "tls_certificates": len(li.get("Certificates", [])),
                    "default_actions": [
                        a.get("Type") for a in li.get("DefaultActions", [])
                    ],
                }
                for li in listeners
            ],
            "target_groups": [
                {
                    "name": g.get("TargetGroupName"),
                    "protocol": g.get("Protocol"),
                    "port": g.get("Port"),
                    "target_type": g.get("TargetType"),
                    "health_check": {
                        "protocol": g.get("HealthCheckProtocol"),
                        "port": g.get("HealthCheckPort"),
                        "path": g.get("HealthCheckPath"),
                    },
                    "targets": [
                        {"id": d["Target"]["Id"], "port": d["Target"].get("Port")}
                        for d in health.get(g["TargetGroupArn"], [])
                    ],
                }
                for g in groups
            ],
        },
        depends_on=targets,
    )


def eks_to_resource(
    cluster: dict[str, Any],
    nodegroups: list[dict[str, Any]],
    specs: dict[str, dict[str, Any]],
    region: str,
) -> SourceResource:
    vpc = cluster.get("resourcesVpcConfig", {})
    return SourceResource(
        id=f"eks:{cluster['name']}",
        name=cluster["name"],
        kind=ResourceKind.KUBERNETES_CLUSTER,
        source_type="aws:eks:cluster",
        region=region,
        tags=cluster.get("tags") or {},
        properties={
            "kubernetes_version": cluster.get("version"),
            "endpoint_public_access": vpc.get("endpointPublicAccess"),
            "vpc_id": vpc.get("vpcId"),
            "node_groups": [
                {
                    "name": ng.get("nodegroupName"),
                    "instance_types": ng.get("instanceTypes") or [],
                    "vcpus": specs.get((ng.get("instanceTypes") or [""])[0], {}).get(
                        "vcpus"
                    ),
                    "memory_mib": specs.get(
                        (ng.get("instanceTypes") or [""])[0], {}
                    ).get("memory_mib"),
                    "min_size": ng.get("scalingConfig", {}).get("minSize"),
                    "max_size": ng.get("scalingConfig", {}).get("maxSize"),
                    "desired_size": ng.get("scalingConfig", {}).get("desiredSize"),
                    "disk_size_gib": ng.get("diskSize"),
                    "ami_type": ng.get("amiType"),
                    "capacity_type": ng.get("capacityType"),
                }
                for ng in nodegroups
            ],
        },
    )
