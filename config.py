from secrets import AIO_USERNAME, AIO_KEY, NOTIFY_URL

BROKER = "io.adafruit.com"

ALERT_TOPIC = ("%s/f/alert" % AIO_USERNAME).encode("utf-8")
CONFIG_TOPIC = ("%s/f/config" % AIO_USERNAME).encode("utf-8")

NOTIFY_TITLE = "ESP32 Motion Alert"
