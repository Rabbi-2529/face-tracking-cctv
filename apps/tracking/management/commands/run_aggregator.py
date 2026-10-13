"""Run the realtime detection aggregator (the `aggregator` container)."""

from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.tracking.aggregator import AggregatorRunner


class Command(BaseCommand):
    help = (
        "Consume the detection event stream and maintain presence intervals "
        "and movement events in realtime."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--consumer",
            default="aggregator-1",
            help="Consumer name within the Redis consumer group. Must be unique "
            "per replica.",
        )
        parser.add_argument("--batch-size", type=int, default=200)
        parser.add_argument(
            "--block-ms",
            type=int,
            default=1000,
            help="How long to block waiting for new events before looping.",
        )

    def handle(self, *args, **options) -> None:
        runner = AggregatorRunner(
            consumer_name=options["consumer"],
            batch_size=options["batch_size"],
            block_ms=options["block_ms"],
        )
        self.stdout.write(
            self.style.SUCCESS(f"aggregator {options['consumer']} starting")
        )
        runner.run()
