# UMATI Energy Device Demo — Documentation

---

## 📘 Table of Contents

1. [Introduction and Context](#1-introduction-and-context)
2. [System Requirements and Installation](#2-system-requirements-and-installation)
3. [Running and Verification](#3-running-and-verification)
4. [UMATI Dashboard](#4-umati-dashboard)
5. [Creating a Custom Device](#5-creating-a-custom-device)

---

## 1. Introduction and Context

This project implements a complete data pipeline from an **energy sensor** to the **UMATI Dashboard** using **OPC UA** and **MQTT**.  
All services are deployed on a single device — a **Raspberry Pi**, which runs the Python client, OPC UA server, Umati Gateway, and the **Mosquitto** MQTT broker.

---

### 1.1 System Architecture

#### Data Flow
```
     ┌──────────────────────────────┐
     │          PZEM Sensor         │
     │ (Voltage, Current, Power)    │
     └──────────────┬───────────────┘
                    │ UART
                    ▼
        ┌────────────────────────────────────────────┐
        │         Raspberry Pi (Single Device)       │
        │────────────────────────────────────────────│
        │                                            │
        │  [Python Client]                           │
        │    └─ Reads data from PZEM via UART        │
        │    └─ Writes data into the OPC UA Server   │
        │                                            │
        │  [OPC UA Server (Sample-Server-node-opcua)]│
        │    └─ Device model EnergyDevice1           │
        │    └─ Receives data from Python Client     │
        │                                            │
        │  [Umati Gateway]                           │
        │    └─ Connects to OPC UA Server            │
        │    └─ Converts data → MQTT and publishes   │
        │                                            │
        │  [Mosquitto Broker]                        │
        │    └─ Receives MQTT from Umati Gateway     │
        │    └─ Local broker and bridge to cloud     │
        │    └─ Ports: 1883 (MQTT), 9001 (WebSocket) │
        └────────────────┬───────────────────────────┘
                         │ MQTT Bridge
                         ▼
                ┌───────────────────────────┐
                │  UMATI Cloud MQTT Broker  │
                │  umati/v2/... topics      │
                └──────────────┬────────────┘
                               │
                               ▼
                ┌─────────────────────────────┐
                │   UMATI Dashboard           │
                │   umati.app                 │
                └─────────────────────────────┘
```

---

### 1.2 Components

| Component | Description |
|------------|-------------|
| **PZEM Sensor** | Measures voltage, current, and power. Connected to the Raspberry Pi via UART. |
| **Python Client** | `RPI-opcua-client/pzem_to_opcua_min.py` — reads data from the sensor and writes it to the OPC UA Server. |
| **OPC UA Server** | `Sample-Server-node-opcua` — hosts the EnergyDevice1 model and exposes nodes. |
| **Umati Gateway** | Reads data from OPC UA and publishes it to MQTT. |
| **Mosquitto** | Persistent MQTT broker (TCP 1883 / WebSocket 9001). Receives MQTT messages and bridges to the UMATI cloud. |
| **UMATI Dashboard** | Displays real-time device data from MQTT. |

> ⚙️ On the Raspberry Pi, the services run continuously:  
> `Python Client → OPC UA Server → Umati Gateway → Mosquitto`.

---

## 2. System Requirements and Installation

---

### 2.1 Hardware

| Component | Minimum Configuration |
|------------|----------------------|
| **Raspberry Pi** | Model 3B+ or newer (recommended: 4 GB RAM) |
| **PZEM Sensor** | PZEM-004T or compatible (UART / TTL) |
| **Cables** | UART → USB adapter (if needed) |
| **Network** | Wi-Fi or Ethernet with access to `umati.app` |

---

### 2.2 Software

| Component | Version / Note |
|------------|----------------|
| **OS** | Raspberry Pi OS / Ubuntu 22.04 LTS |
| **Python** | 3.10 or later |
| **Docker** | 24.0+ |
| **Docker Compose** | v2.20+ |
| **Node.js** | 22+ (optional, if running OPC UA server manually) |

---

### 2.3 Installation and Startup

1. **System Update**
   ```bash
   sudo apt update && sudo apt upgrade -y
   ```
2. **Install Docker and Compose**
   ```bash
   sudo apt install -y docker.io docker-compose
   sudo systemctl enable docker
   sudo usermod -aG docker $USER
   ```
3. **Install Python and Git**
   ```bash
   sudo apt install -y python3-venv python3-pip git
   ```
4. **Clone the Repository**
   ```bash
   git clone https://github.com/DataArt/OPC_UA_umati_energy_meter.git
   cd OPC_UA_umati_energy_meter
   ```
5. **Prepare Python Client**
   ```bash
   cd RPI-opcua-client
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```
6. **Start Containers**
   ```bash
   cd ~/OPC_UA_umati_energy_meter
   docker compose up -d --build
   ```
7. **Verify**
   ```bash
   docker ps
   ```
   > Containers `opcua-server`, `umati-gateway`, and `mosquitto` should be in **Up** state.

---

## 3. Running and Verification

---

### 3.1 Run the Python Client (PZEM → OPC UA)

```bash
cd ~/OPC_UA_umati_energy_meter/RPI-opcua-client
source .venv/bin/activate
python3 pzem_to_opcua_min.py
```

Expected log:
```text
INFO: Starting PZEM → OPC UA writer
INFO: Connecting to OPC UA: opc.tcp://127.0.0.1:4840
INFO: Connected.
```

> When the sensor is connected, values update automatically under `EnergyDevice1`.

---

### 3.2 Configure Umati Gateway (Web UI)

Open:
```
http://<RPi IP>:8080/
```

#### 1️⃣ Connect to OPC UA Server
- Endpoint: `opc.tcp://opcua-server:4840/UA`
- Click **Connect**
- Status: **Connected**

#### 2️⃣ Subscribe to the Device Node
- Tab: **OPCSubscriptions**
- Tree:
  ```
  Root → Objects → EnergyDevice1
  ```
- Select node → **Publish MQTT**
- Device appears in **Published Nodes** under **MQTT Configuration tab**

#### 3️⃣ Connect to MQTT

**Local Testing (Mosquitto)**  
In `umati_app/mosquitto/config/mosquitto.conf`:
```bash
allow_anonymous true
listener 1883
listener 9001
protocol websockets
```

In **Mqtt Connection**:
| Field | Value |
|--------|--------|
| Connection URL | `mqtt://mosquitto:1883` |
| Username/Password | Leave empty |

Click **Connect** → status becomes **Connected** → device appears in **Resolved Nodes**.

---

**Staging/Production**
- In `mosquitto.conf`: set `allow_anonymous false`
- In **Mqtt Connection**:
    - URL: `mqtt://<host>:1883`
    - Credentials: UMATI App login/password

**MQ3T Testing**
- Host: `umati.app`
- Port: `1883`
- Username/Password: UMATI App credentials
- Subscribe: `umati/v2/#`

---

### 3.3 Verify on UMATI Dashboard

```
umati.app
```
> If configured properly, the `EnergyDevice1` device appears with real-time data.

---

### 3.4 Useful Commands

Logs:
```bash
docker compose logs -f opcua-server
docker compose logs -f umati-gateway
docker compose logs -f mosquitto
```

Stop:
```bash
docker compose down
```

Auto-restart (`docker-compose.yml`):
```yaml
restart: always
```

---

## 4. UMATI Dashboard

---

### 4.1 Repository
https://github.com/umati/Dashboard/

UMATI Dashboard visualizes devices that publish data in the UMATI-compliant format via MQTT.  
The visualization type depends on the **TypeDefinition** of the OPC UA object.

---

### 4.2 Why `BaseObjectType` Is Insufficient
- If a device uses `BaseObjectType`, the dashboard only shows identification info.
- To display measurements, the device must use a specific **UMATI Type** with a defined variable tree.

---

### 4.3 Required Type for Energy Data
- Our use case: **`UmatiPlasticsRubberGenericType`**
- Expected child: `Electricity` → `Voltage`, `Current`, `Power`, `Energy`
- Specifications: https://showcase.umati.org/
- Default templates:  
  https://github.com/umati/Dashboard/tree/develop/Dashboard/ClientApp/src/templates/

> Templates can be extended or customized for your own device type.

---

### 4.4 Implementation in This Project
File: `src/machines/EnergyDevice1/energydevice1-nodes.ts`  
This file defines the `EnergyDevice1` object, assigns its **typeDefinition**, and creates the `Electricity` folder with variables.

---

### 4.5 Minimal Example (Node-OPCUA)

```ts
export function buildEnergyDevice1(addressSpace: any) {
  const ns = addressSpace.getOwnNamespace();

  const device = ns.addObject({
    browseName: "EnergyDevice1",
    typeDefinition: ns.findObjectType("UmatiPlasticsRubberGenericType"),
    organizedBy: addressSpace.rootFolder.objects,
  });

  const electricity = ns.addFolder(device, { browseName: "Electricity" });

  ns.addVariable({
    componentOf: electricity,
    browseName: "Voltage",
    dataType: "Double",
    value: { get: () => new Variant({ dataType: DataType.Double, value: 0 }) },
  });
}
```

---

### 4.6 Updating Values
The Python client (`pzem_to_opcua_min.py`) writes values into `EnergyDevice1 → Electricity`.  
When variable names match the specification, the dashboard automatically reflects live data once published through the UmatiGateway.

---

### 4.7 Dashboard Checklist

| Check | Description |
|--------|--------------|
| ✅ | `TypeDefinition = UmatiPlasticsRubberGenericType` |
| ✅ | Folder `Electricity` with expected variables exists |
| ✅ | Variable names match dashboard templates |
| ✅ | Nodes are published in UmatiGateway |
| ✅ | MQTT connection is active |
| ✅ | Data is visible on the Dashboard |

---

## 5. Creating a Custom Device

---

### 5.1 Concept
A new device = **OPC UA Model + Python Client + MQTT Publication**.

```
[Sensor] → [Python Client] → [OPC UA Server] → [Umati Gateway] → [MQTT] → [UMATI Dashboard]
```

---

### 5.2 Project Structure
```
src/
└── machines/
    ├── EnergyDevice1/
    │   └── energydevice1-nodes.ts
    └── <NewDeviceName>/
        └── <newdevice>-nodes.ts
RPI-opcua-client/
└── <newdevice>_client.py
```

---

### 5.3 Define the Device Model (Node-OPCUA)
1. Create folder `src/machines/<NewDeviceName>/`
2. Add file `<newdevice>-nodes.ts` with function `build<NewDeviceName>()`
3. Set `TypeDefinition`, create folders and variables.

**Example:**
```ts
export function buildTemperatureDevice1(addressSpace: any) {
  const ns = addressSpace.getOwnNamespace();

  const device = ns.addObject({
    browseName: "TemperatureDevice1",
    typeDefinition: ns.findObjectType("BaseObjectType"),
    organizedBy: addressSpace.rootFolder.objects,
  });

  const temp = ns.addFolder(device, { browseName: "Temperature" });
  ns.addVariable({
    componentOf: temp,
    browseName: "TempCelsius",
    dataType: "Double",
    value: { get: () => new Variant({ dataType: DataType.Double, value: 0 }) },
  });
}
```

---

### 5.4 Register the Device (`src/addressspace.ts`)
```ts
import { buildTemperatureDevice1 } from "./machines/TemperatureDevice1/temperaturedevice1-nodes";

export function populateAddressSpace(addressSpace: any) {
  buildTemperatureDevice1(addressSpace);
  // buildEnergyDevice1(addressSpace);
}
```
Restart the server:
```bash
docker compose restart opcua-server
```

---

### 5.5 Node Structure Example
```
<NewDeviceName>
 └── Monitoring
     └── <Group>
         └── <Variables...>
```

> Use clear `browseName` identifiers — the Python client resolves nodes by names, not NodeIds.

---

### 5.6 Python Client for a New Device
1. Copy `pzem_to_opcua_min.py` to `<device>_client.py`
2. Update:
    - `machine_root_nodeid`
    - `channel_path`
    - `variables`
3. Adjust data reading logic to your sensor.

**Example `config.json`:**
```json
{
  "opcua": {
    "server_url": "opc.tcp://127.0.0.1:4840",
    "machine_root_nodeid": "ns=1;i=<NODEID>",
    "channel_path": ["Monitoring", "Consumption", "Electricity", "Main"],
    "variables": ["AcVoltagePe","AcCurrentPe","AcActivePowerPe","AcActiveEnergyTotalImportHp"]
  }
}
```

---

### 5.7 Publishing via UmatiGateway
1. Connect to `opc.tcp://opcua-server:4840/UA`
2. In **OPCSubscriptions**, find your new device → **Publish MQTT**
3. Go to **Mqtt Connection** → **Connect**

---

### 5.8 Displaying Data on the Dashboard
1. Assign a valid UMATI TypeDefinition (`UmatiPlasticsRubberGenericType` or custom)
2. Check dashboard templates in:  
   https://github.com/umati/Dashboard/tree/develop/Dashboard/ClientApp/src/templates/
3. Once published, the device appears automatically with its live values.

---

### 5.9 Verification Checklist

| Check | Description |
|--------|--------------|
| ✅ | New `<device>-nodes.ts` file created |
| ✅ | `build<Device>(addressSpace)` is called in `addressspace.ts` |
| ✅ | OPC UA server restarted |
| ✅ | NodeIds and `browseName` match client configuration |
| ✅ | Python client writes values successfully |
| ✅ | Device published in UmatiGateway |
| ✅ | MQTT Connected |
| ✅ | Data visible on Dashboard |
