"""Deployable entrypoint: a Google Cloud Function (2nd gen) triggered
directly by the Gmail-notifications Pub/Sub topic.

Deploy (example):
    gcloud functions deploy deal-intake-pubsub \\
        --gen2 --runtime=python312 --region=<region> \\
        --source=. --entry-point=handle_pubsub_message \\
        --trigger-topic=<GMAIL_WATCH_TOPIC>

Run locally with the Functions Framework emulator:
    functions-framework --target=handle_pubsub_message --signature-type=cloudevent
"""
import asyncio

import functions_framework
from cloudevents.http import CloudEvent

from src.services.gmail_intake_service import GmailIntakeService


@functions_framework.cloud_event
def handle_pubsub_message(cloud_event: CloudEvent) -> None:
    asyncio.run(GmailIntakeService().handle_pubsub_envelope(cloud_event.data))
