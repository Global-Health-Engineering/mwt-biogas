"""Desk integration: live acquisition plus a synthetic model worker."""

"""from concurrent.futures import ProcessPoolExecutor"""
from datetime import datetime, timezone
import json
import multiprocessing
from pathlib import Path
import signal
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pasteurizer.hardware import HardwareReader
from pasteurizer.signals import SignalProcessor
from treatment_model.variable_tracking import VariableCoilTracker
from treatment_model.tracking import summarize_discharge


ACQUISITION_INTERVAL_S = 0.2
MODEL_INTERVAL_S = 1.0
MAX_MODEL_GAP_S = 3.0

# Explicit desk-test inputs, referenced to the tracker's reference density.
SYNTHETIC_INPUTS = {
    "total_flow_lpm": 0.5,
    "inlet_c": 40.0,
    "bath_c": 80.0,
}

worker_trackers = None
worker_settings = None


def initialise_worker(settings):
    """Runs only in the child process; no hardware connection."""
    global worker_trackers, worker_settings

    # Let the parent handle Ctrl+C and shut down the worker.
    signal.signal(signal.SIGINT, signal.SIG_IGN)

    worker_settings = settings
    worker_trackers = [
        VariableCoilTracker(settings, index)
        for index in range(2)
    ]


def advance_model(sequence, duration_s, inputs):
    """Advance the worker-owned model by one synthetic input interval."""

    started = time.perf_counter()
    discharged = []

    fractions = worker_settings["hydraulics"][
        "branch_mass_flow_fractions"
    ]

    try:
        for tracker, fraction in zip(worker_trackers, fractions):
            discharged.extend(
                tracker.advance(
                    duration_s,
                    inputs["total_flow_lpm"] * fraction,
                    inputs["inlet_c"],
                    inputs["bath_c"],
                )
            )

        summary = summarize_discharge(
            discharged,
            worker_settings["target_log_reduction"],
        )

        return {
            "sequence": sequence,
            "input_source": "SYNTHETIC",
            "duration_s": duration_s,
            "inputs": dict(inputs),
            "summary": summary,
            "computation_s": time.perf_counter() - started,
            "parcel_count": sum(
                len(tracker.parcels) for tracker in worker_trackers
            ),
            "flags": sorted(set().union(
                *(tracker.current_flags for tracker in worker_trackers)
            )),
        }

    except Exception:
        for tracker in worker_trackers:
            tracker.invalidate_history()
        raise

def model_worker_loop(connection, settings):
    """Own the model and handle one request at a time."""
    try:
        initialise_worker(settings)

        while True:
            request = connection.recv()
            if request is None:
                return

            try:
                result = advance_model(*request)
                connection.send(("result", result))
            except Exception as error:
                connection.send((
                    "error",
                    f"{type(error).__name__}: {error}",
                ))
                return

    except EOFError:
        return
    finally:
        connection.close()


class ModelWorker:
    """Single model process with bounded shutdown and no job backlog."""

    def __init__(self, settings):
        context = multiprocessing.get_context("spawn")
        self.connection, child_connection = context.Pipe()
        self.process = context.Process(
            target=model_worker_loop,
            args=(child_connection, settings),
            daemon=True,
        )
        self.busy = False

        try:
            self.process.start()
        except Exception:
            self.connection.close()
            raise
        finally:
            child_connection.close()

    def submit(self, function, sequence, duration_s, inputs):
        if self.busy:
            raise RuntimeError("Model worker already has a pending request")
        if not self.process.is_alive():
            raise RuntimeError("Model worker is not running")

        self.connection.send((sequence, duration_s, inputs))
        self.busy = True
        return self

    def done(self):
        return (
            self.connection.poll()
            or not self.process.is_alive()
        )

    def result(self):
        if not self.connection.poll():
            raise RuntimeError(
                f"Model worker exited without a result "
                f"(exit code {self.process.exitcode})"
            )

        try:
            kind, payload = self.connection.recv()
        except EOFError as error:
            raise RuntimeError(
                "Model worker connection closed without a result"
            ) from error
        finally:
            self.busy = False

        if kind == "error":
            raise RuntimeError(payload)
        if kind != "result":
            raise RuntimeError("Unexpected model worker response")
        return payload

    def cancel(self):
        # Running calculations are stopped during shutdown.
        return False

    def shutdown(self, **kwargs):
        # An idle worker can exit normally.
        if self.process.is_alive() and not self.busy:
            try:
                self.connection.send(None)
            except (BrokenPipeError, EOFError, OSError):
                pass
            self.process.join(timeout=0.5)

        # A busy or unresponsive worker must not block shutdown.
        if self.process.is_alive():
            print("Stopping model worker...", flush=True)
            self.process.terminate()
            self.process.join(timeout=1.0)

        if self.process.is_alive():
            print("Force-stopping model worker...", flush=True)
            self.process.kill()
            self.process.join(timeout=1.0)

        still_alive = self.process.is_alive()
        self.connection.close()

        if still_alive:
            raise RuntimeError("Model worker did not stop after kill")

        self.process.close()
        print("Model worker stopped.", flush=True)


def main():
    with (ROOT / "config/treatment_settings.json").open() as file:
        settings = json.load(file)

    fractions = settings["hydraulics"]["branch_mass_flow_fractions"]
    if (
        len(fractions) != 2
        or any(not 0 < fraction <= 1 for fraction in fractions)
        or abs(sum(fractions) - 1.0) > 1e-9
    ):
        raise ValueError("Expected two positive branch fractions summing to one")

    processor = SignalProcessor()
    log_directory = ROOT / "logs"
    log_directory.mkdir(exist_ok=True)

    filename = datetime.now(timezone.utc).strftime(
        "connection_%Y%m%dT%H%M%S_%fZ.jsonl"
    )
    log_path = log_directory / filename

    executor = ModelWorker(settings)

    pending = None
    latest = None
    sequence = 0
    max_computation_s = 0.0
    interruptions = 0
    fault = None

    print("DESK CONNECTION TEST — SYNTHETIC MODEL INPUTS")
    print("Live sensors are logged; model results do not control outputs.")
    print(f"Log: {log_path}", flush=True)

    try:
        with HardwareReader() as hardware, log_path.open("x") as logfile:
            next_sample = time.monotonic()
            next_report = next_sample
            interval_start = None
            pending_endpoint = None
            latest_endpoint = None

            test_pause_done = False

            while True:
                snapshot = hardware.read()


                signals = processor.process(snapshot)
                sampled_at = snapshot["sampled_at"]

                if signals["sampling_interrupted"]:
                    interruptions += 1
                    raise RuntimeError(
                        "Acquisition interrupted; connection test stopped"
                    )

                # Start the synthetic timeline at the first live snapshot.
                if interval_start is None:
                    interval_start = sampled_at

                # Receive completed work without blocking acquisition.
                if pending is not None and pending.done():
                    latest = pending.result()
                    latest_endpoint = pending_endpoint
                    max_computation_s = max(
                        max_computation_s,
                        latest["computation_s"],
                    )
                    pending = None

                # No backlog: allow only one outstanding model job.
                gap = sampled_at - interval_start
                if gap > MAX_MODEL_GAP_S:
                    raise RuntimeError(
                        "Model update gap exceeded 3 seconds; "
                        "connection test stopped"
                    )

                if pending is None and gap >= MODEL_INTERVAL_S:
                    sequence += 1
                    pending_endpoint = sampled_at

                    model_inputs = dict(SYNTHETIC_INPUTS)

                    pending = executor.submit(
                        advance_model,
                        sequence,
                        gap,
                        model_inputs,
                    )
                    interval_start = sampled_at

                now = time.monotonic()
                if now >= next_report:
                    result_age = (
                        None if latest_endpoint is None
                        else now - latest_endpoint
                    )
                    available = (
                        latest is not None
                        and result_age <= MAX_MODEL_GAP_S
                    )

                    record = {
                        "timestamp_utc": datetime.now(
                            timezone.utc
                        ).isoformat(),
                        "snapshot": snapshot,
                        "signals": signals,
                        "model": {
                            "input_source": "SYNTHETIC",
                            "available": available,
                            "pending": pending is not None,
                            "result_age_s": result_age,
                            "result": latest if available else None,
                        },
                    }

                    logfile.write(
                        json.dumps(record, allow_nan=False) + "\n"
                    )
                    logfile.flush()

                    if available:
                        summary = latest["summary"]
                        print(
                            f"SYNTHETIC model #{latest['sequence']}"
                            f" | compute={latest['computation_s']:.4f} s"
                            f" | parcels={latest['parcel_count']}"
                            f" | history={summary['history_complete']}"
                            f" | age={result_age:.2f} s"
                            f" | acquisition interruptions={interruptions}",
                            flush=True,
                        )
                    else:
                        print(
                            "SYNTHETIC model unavailable/waiting"
                            f" | acquisition interruptions={interruptions}",
                            flush=True,
                        )

                    next_report = now + 1.0

                next_sample += ACQUISITION_INTERVAL_S
                now = time.monotonic()
                if next_sample <= now:
                    next_sample = now + ACQUISITION_INTERVAL_S

                time.sleep(max(0.0, next_sample - now))

    except KeyboardInterrupt:
        print("\nConnection monitor stopped.")

    except Exception as error:
        fault = str(error)
        print(f"\nMODEL UNAVAILABLE: {fault}", flush=True)

        # Save the terminal fault separately from previous successful results.
        with log_path.open("a") as logfile:
            logfile.write(json.dumps({
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                "event": "connection_fault",
                "model_available": False,
                "error": fault,
            }) + "\n")

    finally:
        processor.reset()
        if pending is not None:
            pending.cancel()
        executor.shutdown(wait=True, cancel_futures=True)

        print(f"Acquisition interruptions: {interruptions}")
        print(f"Maximum observed model computation: {max_computation_s:.4f} s")
        print(f"Log: {log_path}")

    if fault is not None:
        raise SystemExit(1)


if __name__ == "__main__":
    main()