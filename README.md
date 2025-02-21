# Real-Time Trading Infrastructure with AWS, Kafka & Zerodha Kite API

## 🚀 Project Overview
- **Phase 1:** AWS infrastructure setup with Kafka and Docker.
- **Phase 2:** Integration of live NSE/BSE data from Zerodha Kite API into Kafka for real-time streaming.

## ⚡ Technologies Used
- **Cloud:** AWS EC2, S3
- **Streaming:** Apache Kafka
- **API:** Zerodha Kite API
- **Python Libraries:** FastAPI, kafka-python, pandas, numpy
- **DevOps:** Terraform (IaC), GitHub Actions (CI/CD)

## 🛠️ Steps to Run

### 1. Terraform Setup
```bash
cd terraform/
terraform init
terraform apply -auto-approve