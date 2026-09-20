{{- define "fd.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "fd.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "fd.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
app.kubernetes.io/name: {{ include "fd.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "fd.selectorLabels" -}}
app.kubernetes.io/name: {{ include "fd.name" .root }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{- define "fd.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "fd.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{- define "fd.image" -}}
{{- if .Values.image.digest -}}
{{- printf "%s@%s" .Values.image.repository .Values.image.digest -}}
{{- else -}}
{{- printf "%s:%s" .Values.image.repository (default .Chart.AppVersion .Values.image.tag) -}}
{{- end -}}
{{- end -}}

{{- define "fd.natsUrl" -}}
{{- if .Values.async.natsUrl -}}
{{- .Values.async.natsUrl -}}
{{- else if .Values.nats.enabled -}}
{{- printf "nats://%s-nats.%s.svc:4222" .Release.Name .Release.Namespace -}}
{{- else -}}
{{- fail "async.enabled requires async.natsUrl or nats.enabled=true" -}}
{{- end -}}
{{- end -}}

{{- define "fd.authSecretName" -}}
{{- if .Values.auth.existingSecret -}}{{ .Values.auth.existingSecret }}{{- else -}}{{ include "fd.fullname" . }}-auth{{- end -}}
{{- end -}}

{{/* Environment shared by API and worker. */}}
{{- define "fd.env" -}}
- {name: EB_ENVIRONMENT, value: prod}
- {name: EB_LOG_JSON, value: "true"}
- {name: EB_MODEL_PATH, value: /app/models/face_landmarker.task}
- name: OTEL_EXPORTER_OTLP_PROTOCOL
  value: http/protobuf
{{- if .Values.otel.endpoint }}
- {name: OTEL_EXPORTER_OTLP_ENDPOINT, value: {{ .Values.otel.endpoint | quote }}}
{{- end }}
{{- if .Values.auth.disabled }}
- {name: EB_AUTH_DISABLED, value: "true"}
{{- else }}
- name: EB_API_KEY_HASHES
  valueFrom:
    secretKeyRef:
      name: {{ include "fd.authSecretName" . }}
      key: {{ .Values.auth.existingSecretKey }}
{{- end }}
{{- if .Values.async.enabled }}
- {name: EB_ASYNC_ENABLED, value: "true"}
- {name: EB_NATS_URL, value: {{ include "fd.natsUrl" . | quote }}}
- {name: EB_NATS_REPLICAS, value: {{ .Values.async.replicas | quote }}}
- {name: EB_NATS_STREAM, value: {{ .Values.async.stream | quote }}}
- {name: EB_NATS_CONSUMER, value: {{ .Values.async.consumer | quote }}}
- {name: EB_S3_BUCKET, value: {{ .Values.storage.bucket | quote }}}
- {name: EB_S3_REGION, value: {{ .Values.storage.region | quote }}}
- {name: EB_S3_FORCE_PATH_STYLE, value: {{ .Values.storage.forcePathStyle | quote }}}
{{- if .Values.storage.endpointUrl }}
- {name: EB_S3_ENDPOINT_URL, value: {{ .Values.storage.endpointUrl | quote }}}
{{- end }}
{{- if .Values.storage.serverSideEncryption }}
- {name: EB_S3_SERVER_SIDE_ENCRYPTION, value: {{ .Values.storage.serverSideEncryption | quote }}}
{{- end }}
{{- if .Values.storage.existingSecret }}
- name: EB_S3_ACCESS_KEY_ID
  valueFrom: {secretKeyRef: {name: {{ .Values.storage.existingSecret }}, key: access-key-id}}
- name: EB_S3_SECRET_ACCESS_KEY
  valueFrom: {secretKeyRef: {name: {{ .Values.storage.existingSecret }}, key: secret-access-key}}
{{- end }}
{{- end }}
{{- with .Values.config.extraEnv }}
{{ toYaml . }}
{{- end }}
{{- end -}}
