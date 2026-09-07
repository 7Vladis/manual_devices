#!/bin/bash

cp -r manual_devices-develop/ca.crt manual_devices-develop/docker-compose.yaml manual_devices-develop/.env manual_devices-develop/media manual_devices
mv manual_devices-develop manual_devices_old
mv manual_devices manual_devices-develop