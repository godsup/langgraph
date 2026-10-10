output "web_url" {
  value = "http://${aws_instance.web.public_ip}"
}

output "db_endpoint" {
  value = aws_db_instance.db.address
}

output "db_master_secret_arn" {
  description = "Secrets Manager secret holding the master password."
  value       = aws_db_instance.db.master_user_secret[0].secret_arn
}

output "bucket_name" {
  value = aws_s3_bucket.assets.bucket
}
