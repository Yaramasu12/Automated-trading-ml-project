provider "aws" {
  region = "us-east-2"
}

resource "aws_instance" "kafka_ec2" {
  ami           = "ami-0fc5d935ebf8bc3bc"
  instance_type = "t2.micro"

  vpc_security_group_ids = [aws_security_group.kafka_sg.id]

  user_data = <<-EOF
              #!/bin/bash
              sudo apt update -y
              sudo apt install openjdk-11-jdk python3-pip -y
              pip3 install kiteconnect kafka-python pandas numpy asyncio fastapi uvicorn
              wget https://downloads.apache.org/kafka/latest/kafka.tgz
              tar -xvzf kafka.tgz
              cd kafka_*
              nohup bin/zookeeper-server-start.sh config/zookeeper.properties &
              nohup bin/kafka-server-start.sh config/server.properties &
              sudo apt install docker.io -y
              sudo systemctl start docker
              sudo systemctl enable docker
              docker run hello-world
              EOF

  tags = {
    Name = "Kafka-EC2"
  }
}

resource "aws_security_group" "kafka_sg" {
  name        = "kafka_sg"
  description = "Allow SSH and Kafka access"

  ingress {
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    from_port   = 9092
    to_port     = 9092
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_s3_bucket" "trading_data_lake" {
  bucket = "nse-bse-trading-data-lake"

  tags = {
    Name        = "TradingDataLake"
    Environment = "Dev"
  }
}

resource "aws_s3_bucket_policy" "bucket_policy" {
  bucket = aws_s3_bucket.trading_data_lake.id
  policy = jsonencode({
    Version = "2012-10-17",
    Statement = [
      {
        Effect    = "Allow",
        Principal = {
          AWS = "arn:aws:iam::897722703799:role/trading-s3-role"
        },
        Action    = "s3:*",
        Resource  = [
          "arn:aws:s3:::nse-bse-trading-data-lake",
          "arn:aws:s3:::nse-bse-trading-data-lake/*"
        ]
      }
    ]
  })
}