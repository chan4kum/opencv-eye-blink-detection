FROM prom/prometheus:v3.14.0
COPY prometheus/prometheus.yml prometheus/alerts.yml /etc/prometheus/
