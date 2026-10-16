{{- define "skillpulse.name" -}}
skillpulse
{{- end }}

{{- define "skillpulse.labels" -}}
app.kubernetes.io/name: {{ include "skillpulse.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end }}

{{- define "skillpulse.selectorLabels" -}}
app.kubernetes.io/name: {{ include "skillpulse.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/* Pod-level security context: non-root, RuntimeDefault seccomp (SPEC section 12). */}}
{{- define "skillpulse.podSecurityContext" -}}
# Service-link variables collide with typed SKILLPULSE_* application settings.
enableServiceLinks: false
securityContext:
  runAsNonRoot: true
  runAsUser: 10001
  runAsGroup: 10001
  fsGroup: 10001
  seccompProfile:
    type: RuntimeDefault
{{- end }}

{{/* Container-level security context: no privilege escalation, read-only root FS. */}}
{{- define "skillpulse.containerSecurityContext" -}}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
capabilities:
  drop: ["ALL"]
{{- end }}

{{/*
Common container env: log level plus the two URLs from the app Secret created by
`make secrets`. Workloads append their own extra env (e.g. the MCP bind settings).
*/}}
{{- define "skillpulse.commonEnv" -}}
- name: SKILLPULSE_LOG_LEVEL
  value: {{ .Values.logLevel | quote }}
- name: SKILLPULSE_DATABASE_URL
  valueFrom:
    secretKeyRef:
      name: {{ default .Values.appSecret .databaseSecret }}
      key: SKILLPULSE_DATABASE_URL
- name: SKILLPULSE_REDIS_URL
  valueFrom:
    secretKeyRef:
      name: {{ default .Values.appSecret .databaseSecret }}
      key: SKILLPULSE_REDIS_URL
{{- end }}

{{/* The /tmp emptyDir every workload mounts (read-only root filesystem needs it). */}}
{{- define "skillpulse.tmpVolume" -}}
- name: tmp
  emptyDir: {}
{{- end }}

{{- define "skillpulse.tmpVolumeMount" -}}
- name: tmp
  mountPath: /tmp
{{- end }}

{{/* Image reference shared by every Deployment and the migration Job. */}}
{{- define "skillpulse.image" -}}
image: "{{ .Values.image.repository }}:{{ .Values.image.tag }}"
imagePullPolicy: {{ .Values.image.pullPolicy }}
{{- end }}