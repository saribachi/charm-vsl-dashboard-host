FROM python:3.12-slim
WORKDIR /app
COPY . .
RUN mkdir -p dashboard data/ghl data/wistia data/meta data/dayai
EXPOSE 3000
CMD ["python", "server.py"]
