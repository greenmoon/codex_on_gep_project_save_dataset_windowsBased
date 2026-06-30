@echo off
cd /d "%~dp0"
set "MQTT_HOST=59.124.7.96"
set "MQTT_PORT=1883"
set "MQTT_TOPIC=height_cm"
set "HELPER_PORT=8765"
python native_mqtt_helper.py --listen-port %HELPER_PORT% --mqtt-host "%MQTT_HOST%" --mqtt-port %MQTT_PORT% --topic "%MQTT_TOPIC%"
pause
