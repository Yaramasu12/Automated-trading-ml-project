output "instance_id" {
  description = "Kafka EC2 Instance ID"
  value       = aws_instance.kafka_ec2.id
}

output "s3_bucket_name" {
  description = "S3 Bucket for trading data"
  value       = aws_s3_bucket.trading_data_lake.bucket
}