# Use Home Assistant Base Image
ARG BUILD_FROM
FROM $BUILD_FROM

# Install Python and required packages
RUN apk add --no-cache \
    python3 \
    py3-pip \
    socat \
    bash \
    jq \
    py3-aiohttp \
    py3-paho-mqtt \
    py3-pyserial

# The add-on version (passed by the Home Assistant builder), reported in the OpenBMS Monitor feed
ARG BUILD_VERSION
ENV ADDON_VERSION=${BUILD_VERSION}

# Set the working directory
WORKDIR /usr/src/app

# Application: launcher + seplos_mqtt package
COPY src/ ./
COPY run.sh /

# Make sure entrypoint is executable
RUN chmod a+x /run.sh

# Run Startup-Script
CMD [ "/run.sh" ]