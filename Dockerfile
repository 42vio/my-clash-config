FROM python:3.13.13-alpine3.22

RUN addgroup -g 10001 clash-sub && adduser -D -H -u 10001 -G clash-sub clash-sub
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY clash_sub ./clash_sub
COPY templates ./templates
USER 10001:10001
ENTRYPOINT ["python", "-m"]
