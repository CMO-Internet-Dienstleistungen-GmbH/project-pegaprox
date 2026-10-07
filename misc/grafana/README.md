# PegaProx Grafana Dashboard

## PegaProx API Token
Access to metrics exposed by PegaProx is secured and requires proper authentication. This ensures that only authorized systems or users can retrieve monitoring data from the exporter endpoint.

To authenticate against the exporter, an API token must be provided with each request. This token acts as a credential and is required for all metric queries.

### Obtaining an API Token
It is recommended to create a dedicated technical account for monitoring purposes. Using a separate account improves security, auditability, and avoids unintended side effects from personal user accounts.

Follow these steps to generate a suitable API token:
* Log in to PegaProx with an administrative or authorized user account.
* Navigate to the User Settings section.
* Create a new API key for the technical account.
* Assign read-only permissions to the API key to restrict access strictly to metric retrieval.
* Copy and securely store the generated API token.

## Prometheus Exporter
To collect metrics from PegaProx, you need to configure Prometheus to scrape the built-in exporter endpoint. Since the metrics endpoint is secured, a few additional settings are required compared to a default scrape job.

PegaProx exposes its metrics over HTTPS on port 5000 under the path /api/metrics. Because of this, you must explicitly enable TLS in your Prometheus configuration. In addition, authentication is required, so you need to create an API token in PegaProx and pass it along with each request.

```
- job_name: 'pegaprox'
  metrics_path: /api/metrics
  scheme: https
  authorization:
    type: Bearer
    credentials: pgx_token123token123
  tls_config:
    insecure_skip_verify: true
  static_configs:
    - targets: ['pegaprox01.int.gyptazy.com:5000']
```

### Storage, replication and backup series
Next to the cluster, node and guest gauges the exporter has, per cluster:

| Series | Labels | Where it comes from |
|---|---|---|
| `pegaprox_storage_used_bytes`, `pegaprox_storage_total_bytes`, `pegaprox_storage_active` | `node` (empty for a shared storage), `storage`, `type`, `shared`, `sr_uuid` on XCP-ng | Proxmox VE: one `/cluster/resources?type=storage` per cluster, shared with the health score and the storage overview for 30 seconds. A shared storage is one series, not one per node. XCP-ng: the SR list of the pool. |
| `pegaprox_storage_inactive_nodes` | as above | Nodes that list a shared storage without having it active. |
| `pegaprox_replication_last_sync_timestamp_seconds`, `pegaprox_replication_last_sync_age_seconds`, `pegaprox_replication_fail_count`, `pegaprox_replication_failed`, `pegaprox_replication_enabled` | `job`, `vmid`, `node` (source), `target` | The replication job list and the status of each source node, read at most once a minute. |
| `pegaprox_guest_last_backup_timestamp_seconds`, `pegaprox_guest_last_backup_age_seconds` | the guest labels | The newest backup of each guest in the snapshot lists of the PBS servers linked to the cluster and in the vzdump files on its backup storages, the scan behind the backup column of the VM list, read at most every ten minutes. Templates are left out. |
| `pegaprox_guest_disk_read_bytes_total`, `pegaprox_guest_disk_write_bytes_total` | the guest labels | `/cluster/resources`, next to the network counters. |
| `pegaprox_cluster_source_up` | `source` = `storage`, `replication` or `backups` | 1 when the last read of that source answered in full. |

A timestamp of 0 means never: a replication job that never synced, a guest without a backup. Those have no age series. Replication and backup reads run in the background, so the first scrape after a start has `pegaprox_cluster_source_up` 0 for them. A job whose source node did not answer, and the backup ages of a scan that did not finish, are left out rather than shown as fine.

Examples:
```
pegaprox_storage_used_bytes / pegaprox_storage_total_bytes > 0.9
pegaprox_replication_failed == 1
pegaprox_guest_last_backup_age_seconds > 86400 * 2
time() - pegaprox_guest_last_backup_timestamp_seconds > 86400 * 2
```

## Grafana Dashboard
You can simply import the dashboard or JSON file to your Grafana instance.