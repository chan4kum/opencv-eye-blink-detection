FROM grafana/grafana:13.2.2
COPY compose/grafana/provisioning /etc/grafana/provisioning
COPY compose/grafana/dashboards /var/lib/grafana/dashboards
