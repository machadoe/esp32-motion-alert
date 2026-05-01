"""
Publish fake ESP32 motion events to Adafruit IO.

Run this on your computer, not on the ESP32:
    python simulate_esp.py

Optional examples:
    python simulate_esp.py --device-id sim-esp32-02
    python simulate_esp.py --count 5 --interval 2
    python simulate_esp.py --instances 3 --count 5 --interval 2
    python simulate_esp.py --device-id sim-a --device-id sim-b --count 5
    python simulate_esp.py --instances 3 --armed false --feed-cooldown 3

Config messages should use the same shape as the real ESP32:
    {"device_id": "sim-esp32-01", "config": {"armed": false}}
"""

import argparse
import json
import random
import threading
import time

try:
    import paho.mqtt.client as mqtt
except ImportError as exc:
    raise SystemExit(
        "Missing dependency: paho-mqtt\n"
        "Install it with: python -m pip install paho-mqtt"
    ) from exc

from secrets import AIO_KEY, AIO_USERNAME


BROKER = "io.adafruit.com"
PORT = 1883
START_TIME = time.time()

DEFAULT_CONFIG = {
    "delta_threshold_x": 0.4,
    "delta_threshold_y": 0.4,
    "notification_cooldown": 30,
    "armed": True,
    "window_size": 5,
    "detect_count_required": 2,
    "clear_count_required": 4,
    "sensor_interval": 0.1,
    "delta_threshold_z": 0.6,
    "feed_cooldown": 1,
}


def parse_bool(value):
    normalized = value.strip().lower()
    if normalized in ("1", "true", "yes", "y", "on"):
        return True
    if normalized in ("0", "false", "no", "n", "off"):
        return False

    raise argparse.ArgumentTypeError("Use true or false")


CONFIG_KEYS = (
    "delta_threshold_x",
    "delta_threshold_y",
    "delta_threshold_z",
    "feed_cooldown",
    "notification_cooldown",
    "armed",
    "window_size",
)


BACKWARD_COMPAT_KEYS = {
    "threshold_x": "delta_threshold_x",
    "threshold_y": "delta_threshold_y",
    "threshold_z": "delta_threshold_z",
}


def build_motion_event(device_id, message_num):
    ax = random.uniform(-2.5, 2.5)
    ay = random.uniform(8.5, 10.5)
    az = random.uniform(-2.5, 2.5)

    dx = random.uniform(0.4, 1.4)
    dy = random.uniform(0.05, 0.9)
    dz = random.uniform(0.05, 1.1)

    return {
        "delta": {
            "x": dx,
            "z": dz,
            "y": dy,
        },
        "device_id": device_id,
        "message_num": message_num,
        "event": "motion_detected",
        "timestamp": int(time.time() - START_TIME),
        "acceleration": {
            "x": ax,
            "z": az,
            "y": ay,
        },
    }


def build_config_state_event(device_id, config, reason, config_received):
    return {
        "config": config,
        "reason": reason,
        "device_id": device_id,
        "event": "config_state",
        "uptime_seconds": int(time.time() - START_TIME),
        "config_received": config_received,
    }


def publish_config_state(client, topic, device_id, state, reason):
    with state["lock"]:
        config = dict(state["config"])
        config_received = state["config_received"]

    event = build_config_state_event(device_id, config, reason, config_received)
    payload = json.dumps(event)
    result = client.publish(topic, payload, qos=1, retain=True)
    result.wait_for_publish()
    print(f"[{device_id}] Published to {topic}: {payload}")


def coerce_config_value(key, value):
    if key in (
        "feed_cooldown",
        "notification_cooldown",
        "window_size",
    ):
        return int(value)

    if key == "armed":
        return bool(value)

    return float(value)


def apply_config_update(state, config_update):
    updates = {}

    for old_key, new_key in BACKWARD_COMPAT_KEYS.items():
        if old_key in config_update:
            updates[new_key] = coerce_config_value(new_key, config_update[old_key])

    for key in CONFIG_KEYS:
        if key in config_update:
            updates[key] = coerce_config_value(key, config_update[key])

    with state["lock"]:
        before_config = dict(state["config"])

        for key, value in updates.items():
            if key in ("feed_cooldown", "notification_cooldown") and value < 0:
                continue

            if key == "window_size" and value < 1:
                continue

            state["config"][key] = value

        state["config_received"] = True
        return state["config"] != before_config


def handle_config_message(client, userdata, message):
    device_id = userdata["device_id"]
    state = userdata["state"]
    config_state_topic = userdata["config_state_topic"]

    try:
        msg_str = message.payload.decode("utf-8").strip()
        if not msg_str:
            print(f"[{device_id}] Empty config payload received")
            return

        incoming = json.loads(msg_str)
        if incoming.get("device_id") != device_id:
            print(f"[{device_id}] Ignoring config for device: {incoming.get('device_id')}")
            return

        config_update = incoming.get("config")
        if not isinstance(config_update, dict):
            print(f"[{device_id}] Config payload missing config object")
            return

        print(f"[{device_id}] Received config: {msg_str}")
        changed = apply_config_update(state, config_update)

        if changed:
            publish_config_state(
                client,
                config_state_topic,
                device_id,
                state,
                "config_update",
            )
        else:
            print(f"[{device_id}] Config unchanged; config state not republished")
    except Exception as exc:
        print(f"[{device_id}] Config handling error: {exc}")


def publish_fake_motion(device_id, count, interval, base_config, stop_event, start_delay=0.0):
    alert_topic = f"{AIO_USERNAME}/f/alert"
    config_topic = f"{AIO_USERNAME}/f/config"
    config_state_topic = f"{AIO_USERNAME}/f/config-state"
    client_id = f"esp32-motion-simulator-{device_id}"
    state = {
        "config": dict(base_config),
        "config_received": False,
        "lock": threading.Lock(),
    }

    if start_delay > 0:
        stop_event.wait(start_delay)

    if stop_event.is_set():
        return

    client = mqtt.Client(client_id=client_id)
    client.user_data_set({
        "device_id": device_id,
        "state": state,
        "config_state_topic": config_state_topic,
    })
    client.on_message = handle_config_message
    client.username_pw_set(AIO_USERNAME, AIO_KEY)
    client.connect(BROKER, PORT, keepalive=60)
    client.subscribe(config_topic)
    client.loop_start()
    print(f"[{device_id}] Subscribed to {config_topic}")

    try:
        publish_config_state(client, config_state_topic, device_id, state, "startup")

        message_num = 1
        while not stop_event.is_set() and (count is None or message_num <= count):
            with state["lock"]:
                armed = state["config"]["armed"]
                feed_cooldown = state["config"]["feed_cooldown"]

            if not armed:
                stop_event.wait(interval)
                continue

            event = build_motion_event(device_id, message_num)
            payload = json.dumps(event)
            result = client.publish(alert_topic, payload, qos=1)
            result.wait_for_publish()

            print(f"[{device_id}] Published to {alert_topic}: {payload}")

            message_num += 1
            if count is None or message_num <= count:
                stop_event.wait(max(interval, feed_cooldown))
    finally:
        client.loop_stop()
        client.disconnect()


def get_device_ids(device_ids, instances):
    if device_ids:
        return device_ids

    return [
        f"sim-esp32-{instance_num:02d}"
        for instance_num in range(1, instances + 1)
    ]


def publish_multiple_devices(device_ids, count, interval, config):
    threads = []
    stop_event = threading.Event()

    for index, device_id in enumerate(device_ids):
        thread = threading.Thread(
            target=publish_fake_motion,
            args=(device_id, count, interval, config, stop_event, index * 0.25),
        )
        thread.start()
        threads.append(thread)

    try:
        for thread in threads:
            thread.join()
    except KeyboardInterrupt:
        stop_event.set()
        print("Stopping simulators...")
        for thread in threads:
            thread.join()


def parse_args():
    parser = argparse.ArgumentParser(
        description="Send fake ESP32 motion events to the Adafruit IO alert feed."
    )
    parser.add_argument(
        "--device-id",
        action="append",
        help=(
            "Fake device_id to include in messages. "
            "Use this more than once to simulate specific devices."
        ),
    )
    parser.add_argument(
        "--instances",
        type=int,
        default=1,
        help="Number of fake ESP32 devices to simulate when --device-id is omitted.",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=None,
        help="Number of messages to send. Omit this to send until Ctrl+C.",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=5.0,
        help="Seconds between fake motion messages.",
    )
    parser.add_argument(
        "--delta-threshold-x",
        type=float,
        default=DEFAULT_CONFIG["delta_threshold_x"],
        help="Config-state delta_threshold_x value.",
    )
    parser.add_argument(
        "--delta-threshold-y",
        type=float,
        default=DEFAULT_CONFIG["delta_threshold_y"],
        help="Config-state delta_threshold_y value.",
    )
    parser.add_argument(
        "--delta-threshold-z",
        type=float,
        default=DEFAULT_CONFIG["delta_threshold_z"],
        help="Config-state delta_threshold_z value.",
    )
    parser.add_argument(
        "--feed-cooldown",
        type=int,
        default=DEFAULT_CONFIG["feed_cooldown"],
        help="Config-state feed_cooldown value.",
    )
    parser.add_argument(
        "--notification-cooldown",
        type=int,
        default=DEFAULT_CONFIG["notification_cooldown"],
        help="Config-state notification_cooldown value.",
    )
    parser.add_argument(
        "--armed",
        type=parse_bool,
        default=DEFAULT_CONFIG["armed"],
        help="Config-state armed value: true or false.",
    )
    parser.add_argument(
        "--sensor-interval",
        type=float,
        default=DEFAULT_CONFIG["sensor_interval"],
        help="Config-state sensor_interval value.",
    )
    parser.add_argument(
        "--window-size",
        type=int,
        default=DEFAULT_CONFIG["window_size"],
        help="Config-state window_size value.",
    )
    parser.add_argument(
        "--detect-count-required",
        type=int,
        default=DEFAULT_CONFIG["detect_count_required"],
        help="Config-state detect_count_required value.",
    )
    parser.add_argument(
        "--clear-count-required",
        type=int,
        default=DEFAULT_CONFIG["clear_count_required"],
        help="Config-state clear_count_required value.",
    )
    return parser.parse_args()


def get_config(args):
    return {
        "delta_threshold_x": args.delta_threshold_x,
        "delta_threshold_y": args.delta_threshold_y,
        "notification_cooldown": args.notification_cooldown,
        "armed": args.armed,
        "window_size": args.window_size,
        "detect_count_required": args.detect_count_required,
        "clear_count_required": args.clear_count_required,
        "sensor_interval": args.sensor_interval,
        "delta_threshold_z": args.delta_threshold_z,
        "feed_cooldown": args.feed_cooldown,
    }


def main():
    args = parse_args()
    if args.instances < 1:
        raise SystemExit("--instances must be 1 or higher")

    device_ids = get_device_ids(args.device_id, args.instances)
    publish_multiple_devices(device_ids, args.count, args.interval, get_config(args))


if __name__ == "__main__":
    main()
