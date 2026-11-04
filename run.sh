#!/usr/bin/with-contenv bashio

set -e

bashio::log.info "Starting Seplos MQTT RS485 Add-on..."

# Konfiguration aus Home Assistant laden und als ENV exportieren
export RS485_REMOTE_IP=$(bashio::config 'rs485_remote_ip')
export RS485_REMOTE_PORT=$(bashio::config 'rs485_remote_port')
export SERIAL_INTERFACE=$(bashio::config 'serial_interface')
export SERIAL_BAUDRATE=$(bashio::config 'serial_baudrate')
export NUMBER_OF_PACKS=$(bashio::config 'number_of_packs')
export MIN_CELL_VOLTAGE=$(bashio::config 'min_cell_voltage')
export MAX_CELL_VOLTAGE=$(bashio::config 'max_cell_voltage')
export MQTT_UPDATE_INTERVAL=$(bashio::config 'mqtt_update_interval')
export ENABLE_HA_DISCOVERY_CONFIG=$(bashio::config 'enable_ha_discovery')
export INVERT_HA_DIS_CHARGE_MEASUREMENTS=$(bashio::config 'invert_ha_dis_charge_measurements')
export HA_DISCOVERY_PREFIX=$(bashio::config 'ha_discovery_prefix')
export LOGGING_LEVEL=$(bashio::config 'logging_level')
export MQTT_TOPIC=$(bashio::config 'mqtt_topic')

# MQTT-Konfiguration aus Home Assistant Services
if bashio::services.available "mqtt"; then
    export MQTT_HOST=$(bashio::services mqtt "host")
    export MQTT_PORT=$(bashio::services mqtt "port")
    export MQTT_USERNAME=$(bashio::services mqtt "username")
    export MQTT_PASSWORD=$(bashio::services mqtt "password")
    bashio::log.info "MQTT service configured: ${MQTT_HOST}:${MQTT_PORT}"
else
    bashio::log.error "MQTT service not available!"
    exit 1
fi

# Socat nur starten wenn Remote-IP konfiguriert ist
if bashio::var.has_value "${RS485_REMOTE_IP}"; then
    bashio::log.info "Configuring remote RS485 connection to ${RS485_REMOTE_IP}:${RS485_REMOTE_PORT}"
    socat pty,link=${SERIAL_INTERFACE},raw tcp:${RS485_REMOTE_IP}:${RS485_REMOTE_PORT},retry,interval=.2,forever &
    sleep 2
else
    bashio::log.info "Using local serial interface: ${SERIAL_INTERFACE}"
fi

# Debug-Ausgabe wenn gewünscht
if [[ "${LOGGING_LEVEL}" == "debug" ]]; then
    bashio::log.debug "Configuration loaded:"
    bashio::log.debug "RS485_REMOTE_IP: ${RS485_REMOTE_IP}"
    bashio::log.debug "SERIAL_INTERFACE: ${SERIAL_INTERFACE}"
    bashio::log.debug "NUMBER_OF_PACKS: ${NUMBER_OF_PACKS}"
fi

bashio::log.info "Starting Seplos BMS data fetcher..."

# Python-Script mit ENV-Variablen starten
exec python3 -u /usr/src/app/fetch_bms_data.py