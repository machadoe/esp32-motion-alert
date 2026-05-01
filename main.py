
from machine import I2C, Pin, unique_id
from mpu6050 import MPU
import network
import neopixel
import time
import urequests
import ujson
import ubinascii
from umqtt.simple import MQTTClient
from secrets import (
    WIFI_SSID,
    WIFI_PASSWORD,
    AIO_USERNAME,
    AIO_KEY,
    NOTIFY_URL,
)

# =========================
# WiFi / Adafruit IO config
# =========================
BROKER = "io.adafruit.com"

ALERT_TOPIC = ("%s/f/alert" % AIO_USERNAME).encode("utf-8")
CONFIG_TOPIC = ("%s/f/config" % AIO_USERNAME).encode("utf-8")
CONFIG_STATE_TOPIC = ("%s/f/config-state" % AIO_USERNAME).encode("utf-8")

NOTIFY_TITLE = "ESP32 Motion Alert"
DEVICE_ID = ubinascii.hexlify(unique_id()).decode("utf-8")

# =========================
# Default runtime settings
# These can be overridden by JSON from the config feed
# =========================
delta_threshold_x = 0.40
delta_threshold_y = 0.40
delta_threshold_z = 0.60

feed_cooldown = 1
notification_cooldown = 30

armed = True

# Sampling / motion logic
sensor_interval = 0.1              # seconds between samples
window_size = 5                    # compare against ~0.5 sec ago if sensor_interval=0.1

detect_count_required = 2
clear_count_required = 4

# NeoPixel settings
# Adafruit ESP32 Feather V2 onboard NeoPixel:
# data pin = GPIO 0, power enable pin = GPIO 2
NEOPIXEL_PIN = 0
NEOPIXEL_POWER_PIN = 2
NEOPIXEL_COUNT = 1
NEOPIXEL_BRIGHTNESS = 32
MOTION_PIXEL_COLOR = (0, NEOPIXEL_BRIGHTNESS, 0)
PIXEL_OFF_COLOR = (0, 0, 0)
STARTUP_TEST_ENABLED = True
STARTUP_TEST_COLOR = (NEOPIXEL_BRIGHTNESS, NEOPIXEL_BRIGHTNESS, NEOPIXEL_BRIGHTNESS)
STARTUP_TEST_DURATION = 0.5

# =========================
# State variables
# =========================
config_received = False

last_feed_time = 0
last_notification_time = 0

motion_count = 0
clear_count = 0
motion_latched = False
message_num = 1

client = None
mpu = None
pixel = None
pixel_power = None
pixel_is_on = None

# history buffer for acceleration samples
sample_history = []

# =========================
# Connectivity
# =========================
def connect_wifi():
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)

    if not wlan.isconnected():
        print("Connecting WiFi...")
        wlan.connect(WIFI_SSID, WIFI_PASSWORD)
        while not wlan.isconnected():
            time.sleep(0.5)

    print("Connected:", wlan.ifconfig())
    return wlan

def mqtt_callback(topic, msg):
    global config_received
    global delta_threshold_x, delta_threshold_y, delta_threshold_z
    global feed_cooldown, notification_cooldown
    global armed
    global window_size

    try:
        if topic == CONFIG_TOPIC:
            msg_str = msg.decode("utf-8").strip()
            print("Received config:", msg_str)

            if not msg_str:
                print("Empty config payload received")
                return

            message = ujson.loads(msg_str)

            if message.get("device_id") != DEVICE_ID:
                print("Ignoring config for device:", message.get("device_id"))
                return

            config = message.get("config")
            if not isinstance(config, dict):
                print("Config payload missing config object")
                return

            before_config = get_config_values()

            if "delta_threshold_x" in config:
                delta_threshold_x = float(config["delta_threshold_x"])

            if "delta_threshold_y" in config:
                delta_threshold_y = float(config["delta_threshold_y"])

            if "delta_threshold_z" in config:
                delta_threshold_z = float(config["delta_threshold_z"])

            # Optional backward compatibility with old names
            if "threshold_x" in config:
                delta_threshold_x = float(config["threshold_x"])

            if "threshold_y" in config:
                delta_threshold_y = float(config["threshold_y"])

            if "threshold_z" in config:
                delta_threshold_z = float(config["threshold_z"])

            if "feed_cooldown" in config:
                value = int(config["feed_cooldown"])
                if value >= 0:
                    feed_cooldown = value

            if "notification_cooldown" in config:
                value = int(config["notification_cooldown"])
                if value >= 0:
                    notification_cooldown = value

            if "armed" in config:
                armed = bool(config["armed"])

            if "window_size" in config:
                value = int(config["window_size"])
                if value >= 1:
                    window_size = value

            after_config = get_config_values()
            config_received = True

            print("Applied config:")
            print(" delta_threshold_x =", delta_threshold_x)
            print(" delta_threshold_y =", delta_threshold_y)
            print(" delta_threshold_z =", delta_threshold_z)
            print(" window_size =", window_size)
            print(" feed_cooldown =", feed_cooldown)
            print(" notification_cooldown =", notification_cooldown)
            print(" armed =", armed)
            if after_config != before_config:
                publish_config_state(client, "config_update")
            else:
                print("Config unchanged; config state not republished")

    except Exception as e:
        print("Config handling error:", e)

def connect_mqtt():
    client = MQTTClient(
        client_id=("esp32-motion-client-" + DEVICE_ID),
        server=BROKER,
        user=AIO_USERNAME,
        password=AIO_KEY,
        keepalive=60
    )
    client.set_callback(mqtt_callback)
    client.connect()
    client.subscribe(CONFIG_TOPIC)
    print("MQTT connected and subscribed to config")
    return client

def wait_for_initial_config(client, timeout_seconds=3):
    start = time.time()
    while time.time() - start < timeout_seconds:
        client.check_msg()
        time.sleep(0.1)

# =========================
# Notification / publish
# =========================
def trigger_notification(message):
    response = None
    try:
        print("Triggering phone notification...")
        body = ujson.dumps({
            "title": NOTIFY_TITLE,
            "message": message,
        })
        response = urequests.post(
            NOTIFY_URL,
            data=body,
            headers={"Content-Type": "application/json"}
        )
        print("Notification server response:", response.status_code)
    except Exception as e:
        print("Notification trigger failed:", e)
    finally:
        if response is not None:
            response.close()

def publish_motion_event(client, event):
    payload = ujson.dumps(event)
    client.publish(ALERT_TOPIC, payload.encode("utf-8"), qos=1)
    print("MQTT publish successful:", payload)

def get_config_values():
    return {
        "delta_threshold_x": delta_threshold_x,
        "delta_threshold_y": delta_threshold_y,
        "delta_threshold_z": delta_threshold_z,
        "feed_cooldown": feed_cooldown,
        "notification_cooldown": notification_cooldown,
        "armed": armed,
        "sensor_interval": sensor_interval,
        "window_size": window_size,
        "detect_count_required": detect_count_required,
        "clear_count_required": clear_count_required,
    }

def get_config_state_event(reason):
    return {
        "event": "config_state",
        "reason": reason,
        "device_id": DEVICE_ID,
        "uptime_seconds": time.time(),
        "config_received": config_received,
        "config": get_config_values(),
    }

def publish_config_state(client, reason):
    try:
        payload = ujson.dumps(get_config_state_event(reason))
        client.publish(CONFIG_STATE_TOPIC, payload.encode("utf-8"), retain=True, qos=1)
        print("Config state published:", payload)
    except Exception as e:
        print("Config state publish failed:", e)

def init_neopixel():
    global pixel
    global pixel_power

    try:
        pixel_power = Pin(NEOPIXEL_POWER_PIN, Pin.OUT)
        pixel_power.value(1)
        pixel = neopixel.NeoPixel(Pin(NEOPIXEL_PIN, Pin.OUT), NEOPIXEL_COUNT)
        set_neopixel(False)
        print(
            "NeoPixel ready on pin",
            NEOPIXEL_PIN,
            "with power pin",
            NEOPIXEL_POWER_PIN
        )
        if STARTUP_TEST_ENABLED:
            run_neopixel_startup_test()
    except Exception as e:
        pixel = None
        pixel_power = None
        print("NeoPixel init failed:", e)

def set_neopixel(enabled):
    global pixel_is_on

    if pixel is None or pixel_is_on == enabled:
        return

    color = MOTION_PIXEL_COLOR if enabled else PIXEL_OFF_COLOR

    for index in range(NEOPIXEL_COUNT):
        pixel[index] = color

    pixel.write()
    pixel_is_on = enabled

def show_neopixel_color(color):
    global pixel_is_on

    if pixel is None:
        return

    for index in range(NEOPIXEL_COUNT):
        pixel[index] = color

    pixel.write()
    pixel_is_on = (color != PIXEL_OFF_COLOR)

def run_neopixel_startup_test():
    print("Running NeoPixel startup test...")
    show_neopixel_color(STARTUP_TEST_COLOR)
    time.sleep(STARTUP_TEST_DURATION)
    set_neopixel(False)

# =========================
# Motion helpers
# =========================
def add_sample(ax, ay, az):
    sample_history.append((ax, ay, az))
    max_len = window_size + 1
    if len(sample_history) > max_len:
        sample_history.pop(0)

def get_window_delta():
    if len(sample_history) < window_size + 1:
        return 0.0, 0.0, 0.0

    old_ax, old_ay, old_az = sample_history[0]
    new_ax, new_ay, new_az = sample_history[-1]

    dx = abs(new_ax - old_ax)
    dy = abs(new_ay - old_ay)
    dz = abs(new_az - old_az)

    return dx, dy, dz

def is_motion(dx, dy, dz):
    return (
        dx > delta_threshold_x or
        dy > delta_threshold_y or
        dz > delta_threshold_z
    )

# =========================
# Main setup
# =========================
i2c = I2C(0, scl=Pin(14), sda=Pin(22))

mpu = MPU(i2c)
init_neopixel()

connect_wifi()

client = connect_mqtt()

wait_for_initial_config(client, timeout_seconds=3)
publish_config_state(client, "startup")

print("System ready.")
print("Device ID =", DEVICE_ID)
print("Config received at startup =", config_received)
print("Monitoring for motion using continuous acceleration change...")

# Seed history
for _ in range(window_size + 1):
    ax, ay, az = mpu.acceleration()
    add_sample(ax, ay, az)
    time.sleep(sensor_interval)

# =========================
# Main loop
# =========================
while True:
    try:
        client.check_msg()

        ax, ay, az = mpu.acceleration()
        add_sample(ax, ay, az)

        dx, dy, dz = get_window_delta()
        now = time.time()

        if armed:
            moving = is_motion(dx, dy, dz)

            if moving:
                motion_count += 1
                clear_count = 0
            else:
                motion_count = 0
                clear_count += 1

            motion_active = motion_latched or (motion_count >= detect_count_required)

            if not motion_latched and motion_count >= detect_count_required:
                motion_latched = True
                set_neopixel(True)
                print(
                    "Motion detected! "
                    "X={:.2f} Y={:.2f} Z={:.2f} | "
                    "dX={:.2f} dY={:.2f} dZ={:.2f}".format(
                        ax, ay, az, dx, dy, dz
                    )
                )
            
            if motion_active:
                sent_update = False

                if now - last_feed_time >= feed_cooldown:                                    
                    event = {
                        "event": "motion_detected",
                        "device_id": DEVICE_ID,
                        "message_num": message_num,
                        "timestamp": now,
                        "acceleration": {
                            "x": ax,
                            "y": ay,
                            "z": az,
                        },
                        "delta": {
                            "x": dx,
                            "y": dy,
                            "z": dz,
                        },
                    }

                    publish_motion_event(client, event)
                    last_feed_time = now
                    sent_update = True

                if now - last_notification_time >= notification_cooldown:
                    notification_message = (
                        "Device {} | Motion {}: X={:.2f} Y={:.2f} Z={:.2f} | "
                        "dX={:.2f} dY={:.2f} dZ={:.2f}"
                    ).format(DEVICE_ID, message_num, ax, ay, az, dx, dy, dz)
                    trigger_notification(notification_message)
                    last_notification_time = now
                    sent_update = True

                if sent_update:
                    message_num += 1

            if motion_latched and clear_count >= clear_count_required:
                motion_latched = False
                set_neopixel(False)
                print("Motion cleared.")

        else:
            if motion_latched or motion_count != 0 or clear_count != 0:
                motion_latched = False
                motion_count = 0
                clear_count = 0
                set_neopixel(False)

        time.sleep(sensor_interval)

    except Exception as e:
        print("Error:", e)
        time.sleep(2)

        try:
            connect_wifi()
            client = connect_mqtt()
            wait_for_initial_config(client, timeout_seconds=3)
        except Exception as reconnect_error:
            print("Reconnect failed:", reconnect_error)
            time.sleep(2)
            
            
            
            
