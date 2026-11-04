#!/usr/bin/with-contenv bashio

set -e

bashio::log.info "Starting Seplos MQTT RS485 Add-on..."

# Add-on options -> environment (assign first, so a failing lookup stops the script under set -e)
export_option() {
    local value
    value=$(bashio::config "$2")
    export "$1=${value}"
}
# Legacy connection options (migration input only): exported only while set, so the Python defaults apply
# otherwise; the app removes them from the add-on configuration after the migration.
export_legacy_option() {
    if bashio::config.has_value "$2"; then
        export_option "$1" "$2"
    fi
}
export_legacy_option RS485_REMOTE_IP rs485_remote_ip
export_legacy_option RS485_REMOTE_PORT rs485_remote_port
export_legacy_option SERIAL_INTERFACE serial_interface
export_legacy_option SERIAL_BAUDRATE serial_baudrate
export_legacy_option NUMBER_OF_PACKS number_of_packs
export_legacy_option MIN_CELL_VOLTAGE min_cell_voltage
export_legacy_option MAX_CELL_VOLTAGE max_cell_voltage
export_legacy_option NOMINAL_CELL_VOLTAGE nominal_cell_voltage
export_legacy_option MQTT_UPDATE_INTERVAL mqtt_update_interval
export_legacy_option MAINT_FIRMWARE maint_firmware
export_option ENABLE_HA_DISCOVERY_CONFIG enable_ha_discovery
export_option INVERT_HA_DIS_CHARGE_MEASUREMENTS invert_ha_dis_charge_measurements
export_option HA_DISCOVERY_PREFIX ha_discovery_prefix
export_option LOGGING_LEVEL logging_level
export_option MQTT_TOPIC mqtt_topic

# MQTT credentials from the Home Assistant services
if bashio::services.available "mqtt"; then
    MQTT_HOST=$(bashio::services mqtt "host")
    MQTT_PORT=$(bashio::services mqtt "port")
    MQTT_USERNAME=$(bashio::services mqtt "username")
    MQTT_PASSWORD=$(bashio::services mqtt "password")
    export MQTT_HOST MQTT_PORT MQTT_USERNAME MQTT_PASSWORD
    bashio::log.info "MQTT service configured: ${MQTT_HOST}:${MQTT_PORT}"
else
    bashio::log.error "MQTT service not available!"
    exit 1
fi

# The connection options above are only read once, to create /data/monitoring.json; afterwards the
# settings app (Ingress) owns them. Python starts the socat bridge for a remote RS485 gateway.
INGRESS_PORT=$(bashio::addon.ingress_port)
export INGRESS_PORT
export MONITORING_FILE=/data/monitoring.json

if [[ "${LOGGING_LEVEL}" == "debug" ]]; then
    bashio::log.debug "Configuration loaded:"
    bashio::log.debug "RS485_REMOTE_IP: ${RS485_REMOTE_IP:-}"
    bashio::log.debug "SERIAL_INTERFACE: ${SERIAL_INTERFACE:-}"
    bashio::log.debug "NUMBER_OF_PACKS: ${NUMBER_OF_PACKS:-}"
fi

bashio::log.info "Starting Seplos BMS data fetcher..."

# exec, so the Python process receives the Supervisor's signals
exec python3 -u /usr/src/app/fetch_bms_data.py
