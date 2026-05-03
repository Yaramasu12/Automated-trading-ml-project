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

# AI-Powered Trading System for NSE/BSE  
*Real-time automated trading platform with AI/ML decision-making*  
[![AWS Free Tier](https://img.shields.io/badge/AWS-Free%20Tier-orange)](https://aws.amazon.com/free/)
[![Python 3.10](https://img.shields.io/badge/Python-3.10-blue)](https://python.org)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow)](LICENSE)

## 🚀 Features
- **Real-time trading** for equities & options (NSE/BSE)
- **AI-driven strategies** (LSTM, GARCH, BERT models)
- **Dynamic risk management** (10% max drawdown enforcement)
- **AWS Free Tier optimized** infrastructure
- **Multi-leg options strategies** with auto-hedging

## 🏗 Architecture
```plantuml
@startuml
node "AWS EC2 t2.micro" {
  component Kafka
  component FastAPI
  component LSTM_Model
  component Risk_Engine
}

database PostgreSQL
database MongoDB
cloud AWS_S3

[Zerodha Kite API] --> Kafka : Market Data
Kafka --> FastAPI : Process
FastAPI --> PostgreSQL : Store Trades
LSTM_Model --> FastAPI : Predictions
FastAPI --> [Zerodha] : Execute Orders
Risk_Engine --> FastAPI : Monitor
AWS_S3 --> LSTM_Model : Historical Data
@enduml
