FROM python:2.7-alpine
RUN pip install docker boto3 requests
WORKDIR /
ADD handler.py initialize.sh /
ENTRYPOINT ["python", "handler.py"]
