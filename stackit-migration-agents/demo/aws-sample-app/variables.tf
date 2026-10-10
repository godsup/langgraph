variable "region" {
  description = "AWS region of the demo source environment."
  type        = string
  default     = "eu-central-1"
}

variable "name_prefix" {
  description = "Prefix for resource names."
  type        = string
  default     = "smd"
}

variable "admin_cidr" {
  description = "Your IPv4 address as /32. SSH and PostgreSQL are only reachable from here."
  type        = string

  validation {
    condition     = can(cidrhost(var.admin_cidr, 0)) && var.admin_cidr != "0.0.0.0/0"
    error_message = "admin_cidr must be a valid IPv4 CIDR and not 0.0.0.0/0."
  }
}

variable "instance_type" {
  description = "EC2 instance type of the web server."
  type        = string
  default     = "t3.micro"
}

variable "db_instance_class" {
  description = "RDS instance class."
  type        = string
  default     = "db.t3.micro"
}

variable "postgres_version" {
  description = "PostgreSQL major version."
  type        = string
  default     = "16"
}

variable "sample_objects" {
  description = "Number of sample objects in the S3 bucket."
  type        = number
  default     = 25
}
