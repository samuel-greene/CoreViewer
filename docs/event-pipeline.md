# Optional event-driven catalog integration

The local viewer does not require a message broker. This integration is for a
separate catalog worker that indexes individual completed outputs instead of
periodically scanning the entire image library.

```text
external producer -> Event Grid -> Service Bus ome-generated queue
                                           |
                                           v
                                    catalog worker -> SQL catalog
                                           |
                                           v
                                    mounted image library
```

## Finalized-image contract

Publish a version 1 Event Grid event after closing the TIFF and atomically moving
it to its final location. Use share-relative paths and stable event IDs on retries.
The following values are synthetic examples:

```json
{
  "id": "demo-job:attempt-1",
  "eventType": "CoreView.OmeGenerated",
  "subject": "ome-tiff-share/demo/synthetic-demo.tif",
  "eventTime": "2026-01-01T00:00:00Z",
  "dataVersion": "1",
  "data": {
    "storageAccount": "examplestorage",
    "shareName": "ome-tiff-share",
    "outputPath": "demo/synthetic-demo.tif",
    "sizeBytes": 123456
  }
}
```

The account must match `COREVIEW_STORAGE_ACCOUNT`. The share name is part of the
API contract. The mounted file must exist and its actual size must equal
`sizeBytes`; replace the illustrative length above. Absolute paths, traversal,
hidden paths, symlinks, and unsupported suffixes are rejected. Never include
credentials, SAS URLs, or image bytes in the event.

An optional `data.job` object can link an existing viewer submission to its
completed output. It must have matching `output_path` and `status: "completed"`.

The catalog commits before acknowledging the notification. Duplicate delivery
preserves curation and review state. Malformed events are dead-lettered. Temporary
indexing failures retry under a renewed lock before returning the message for
redelivery. Event mode does not reconcile missing or renamed images; use explicit
`once` and `validate` commands when needed.

## Worker configuration

Configure `DATABASE_URL`, `IMAGES_DIR`, `COREVIEW_STORAGE_ACCOUNT`, and
`CATALOG_SERVICE_BUS_QUEUE=ome-generated` in the worker environment. Authenticate
with either a queue-scoped Listen connection string in
`CATALOG_SERVICE_BUS_CONNECTION_STRING`, or a managed identity plus
`SERVICE_BUS_NAMESPACE` and a Data Receiver grant on the queue.

Initialize the catalog, then run one receiver:

```bash
python -m scripts.catalog_worker init
python -m scripts.catalog_worker events
```

The viewer web process needs no broker credentials. Keep the worker's image mount
and database consistent with the viewer.

## Infrastructure example

`infra/pipeline-events.bicep` defines a custom Event Grid topic, Standard Service
Bus namespace, upload/completion queues, routing subscriptions, queue-scoped
Listen rules, and a private Blob dead-letter container. Supply your own existing
`storageAccountName` and choose names for your environment. The template contains
no deployed resource inventory or credentials. Provisioning is optional and is
not part of the local demo or CI.

The upload route uses `CoreView.FileUpload`; its producer and conversion consumer
are external to this repository. Only finalized-image consumption is implemented
here.
