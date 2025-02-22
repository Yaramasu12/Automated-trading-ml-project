import boto3
import json
from datetime import datetime

s3 = boto3.client('s3', region_name='us-east-2')

def upload_to_s3(data, bucket='nse-bse-trading-data-lake'):
    filename = f"data_{datetime.now().strftime('%Y%m%d%H%M%S')}.json"
    s3.put_object(Bucket=bucket, Key=filename, Body=json.dumps(data))
    print(f"Uploaded {filename} to S3")