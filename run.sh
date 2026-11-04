#!/usr/bin/with-contenv bashio

set -e

bashio::log.info "Starting Seplos MQTT RS485 Add-on..."

# Add-on options -> environment (assign first, so a failing lookup stops the script under set -e)
export_option() {
    local value
    value=$(bashio::config "$2")
    export "$1=${value}"
}
export_option RS485_REMOTE_IP rs485_remote_ip
export_option RS485_REMOTE_PORT rs485_remote_port
export_option SERIAL_INTERFACE serial_interface
export_option SERIAL_BAUDRATE serial_baudrate
export_option NUMBER_OF_PACKS number_of_packs
export_option MIN_CELL_VOLTAGE min_cell_voltage
export_option MAX_CELL_VOLTAGE max_cell_voltage
export_option NOMINAL_CELL_VOLTAGE nominal_cell_voltage
export_option MQTT_UPDATE_INTERVAL mqtt_update_interval
export_option ENABLE_HA_DISCOVERY_CONFIG enable_ha_discovery
export_option INVERT_HA_DIS_CHARGE_MEASUREMENTS invert_ha_dis_charge_measurements
export_option HA_DISCOVERY_PREFIX ha_discovery_prefix
export_option MAINT_FIRMWARE maint_firmware
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

# Bridge a remote RS485-to-Ethernet gateway to a local pty
if bashio::var.has_value "${RS485_REMOTE_IP}"; then
    bashio::log.info "Configuring remote RS485 connection to ${RS485_REMOTE_IP}:${RS485_REMOTE_PORT}"
    socat "pty,link=${SERIAL_INTERFACE},raw" "tcp:${RS485_REMOTE_IP}:${RS485_REMOTE_PORT},retry,interval=.2,forever" &
    SOCAT_PID=$!
    # wait (bounded) for the pty link
    for _ in $(seq 1 50); do
        [ -e "${SERIAL_INTERFACE}" ] && break
        kill -0 "${SOCAT_PID}" 2>/dev/null || { bashio::log.error "socat exited"; exit 1; }
        sleep 0.1
    done
    [ -e "${SERIAL_INTERFACE}" ] || { bashio::log.error "pty ${SERIAL_INTERFACE} not created"; exit 1; }
else
    bashio::log.info "Using local serial interface: ${SERIAL_INTERFACE}"
fi

if [[ "${LOGGING_LEVEL}" == "debug" ]]; then
    bashio::log.debug "Configuration loaded:"
    bashio::log.debug "RS485_REMOTE_IP: ${RS485_REMOTE_IP}"
    bashio::log.debug "SERIAL_INTERFACE: ${SERIAL_INTERFACE}"
    bashio::log.debug "NUMBER_OF_PACKS: ${NUMBER_OF_PACKS}"
fi

bashio::log.info "Starting Seplos BMS data fetcher..."

# exec, so the Python process receives the Supervisor's signals
exec python3 -u /usr/src/app/fetch_bms_data.py
