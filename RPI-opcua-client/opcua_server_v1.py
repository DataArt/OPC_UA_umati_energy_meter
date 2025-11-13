#!/usr/bin/env python3
"""
Raspberry Pi PZEM OPC-UA Client
Reads PZEM data and sends to OPC-UA Server
"""

import asyncio
import logging
import time
import threading
import serial
import struct
from datetime import datetime
from asyncua import Client

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
logger = logging.getLogger(__name__)

# Configuration
OPCUA_SERVER_URL = "opc.tcp://192.168.1.207:4840/UA"
SAMPLE_INTERVAL = 2.0  # seconds

class PZEMReader:
    """Your existing PZEM reader - adapted from your server code"""

    def __init__(self, device='/dev/ttyAMA0'):
        self.device = device
        self.latest_data = {}
        self.running = False

    def decode_pzem_response(self, response_hex):
        if isinstance(response_hex, str):
            data = bytes.fromhex(response_hex.replace(' ', ''))
        else:
            data = response_hex

        if len(data) < 25:
            return "Response too short"

        try:
            voltage = struct.unpack('>H', data[3:5])[0] / 10.0
            current = struct.unpack('>I', b'\x00' + data[5:8])[0] / 1000.0
            power = struct.unpack('>I', b'\x00' + data[9:12])[0] / 10.0
            energy = struct.unpack('>I', b'\x00' + data[13:16])[0]
            frequency = struct.unpack('>H', data[17:19])[0] / 10.0
            power_factor = struct.unpack('>H', data[19:21])[0] / 100.0

            return {
                'voltage': voltage,
                'current': current,
                'power': power,
                'energy': energy,
                'frequency': frequency,
                'power_factor': power_factor,
                'timestamp': time.time(),
                'status': 'OK'
            }
        except Exception as e:
            return f"Decode error: {e}"

    def read_pzem_data(self):
        try:
            ser = serial.Serial(self.device, 9600, timeout=2)
            command = bytes([0x01, 0x04, 0x00, 0x00, 0x00, 0x0A, 0x70, 0x0D])
            ser.write(command)
            time.sleep(0.2)
            response = ser.read(25)
            ser.close()

            if response:
                return self.decode_pzem_response(response)
            else:
                return {"status": "No response"}
        except Exception as e:
            return {"status": f"Error: {e}"}

    def start_reading(self):
        self.running = True
        thread = threading.Thread(target=self._read_loop)
        thread.daemon = True
        thread.start()
        logger.info("PZEM reading started")

    def _read_loop(self):
        while self.running:
            try:
                data = self.read_pzem_data()
                if isinstance(data, dict) and 'voltage' in data:
                    self.latest_data = data
                    logger.info(f"PZEM: {data['voltage']:.1f}V, {data['current']:.3f}A, {data['power']:.1f}W")
                else:
                    self.latest_data = {"status": str(data)}
                    logger.warning(f"PZEM error: {data}")
            except Exception as e:
                logger.error(f"PZEM read error: {e}")
                self.latest_data = {"status": f"Error: {e}"}
            time.sleep(2)

    def get_latest_data(self):
        return self.latest_data

class EnergyMonitor:
    def __init__(self):
        self.opcua_client = None
        self.pzem_reader = PZEMReader(device='/dev/ttyAMA0')  # Using your existing PZEM reader
        self.nodes = {}

    def get_pzem_data(self):
        """Get current PZEM readings"""
        return self.pzem_reader.get_latest_data()

    async def connect_opcua(self):
        """Connect to Windows OPC-UA server"""
        try:
            logger.info(f"Connecting to OPC-UA server: {OPCUA_SERVER_URL}")
            self.opcua_client = Client(url=OPCUA_SERVER_URL)
            await self.opcua_client.connect()

            # Get node references
            self.nodes = {
                'voltage': self.opcua_client.get_node("ns=1;s=VirtualEnergyMeter.Voltage"),
                'current': self.opcua_client.get_node("ns=1;s=VirtualEnergyMeter.Current"),
                'power': self.opcua_client.get_node("ns=1;s=VirtualEnergyMeter.Power"),
                'energy': self.opcua_client.get_node("ns=1;s=VirtualEnergyMeter.Energy"),
                'frequency': self.opcua_client.get_node("ns=1;s=VirtualEnergyMeter.Frequency"),
                'power_factor': self.opcua_client.get_node("ns=1;s=VirtualEnergyMeter.PowerFactor"),
                'status': self.opcua_client.get_node("ns=1;s=VirtualEnergyMeter.Status"),
                'timestamp': self.opcua_client.get_node("ns=1;s=VirtualEnergyMeter.LastUpdate")
            }

            logger.info("✅ Connected to OPC-UA server")
            return True

        except Exception as e:
            logger.error(f"❌ OPC-UA connection failed: {e}")
            return False

    async def send_data_to_opcua(self, data):
        """Send PZEM data to OPC-UA server"""
        try:
            # Convert all values to proper types for OPC-UA
            voltage = float(data.get('voltage', 0.0))
            current = float(data.get('current', 0.0))
            power = float(data.get('power', 0.0))
            energy = float(data.get('energy', 0.0))
            frequency = float(data.get('frequency', 50.0))
            power_factor = float(data.get('power_factor', 1.0))
            timestamp_str = datetime.now().isoformat()
            status_str = str(data.get('status', 'OK'))

            # Send all data to OPC-UA nodes
            await self.nodes['voltage'].write_value(voltage)
            await self.nodes['current'].write_value(current)
            await self.nodes['power'].write_value(power)
            await self.nodes['energy'].write_value(energy)
            await self.nodes['frequency'].write_value(frequency)
            await self.nodes['power_factor'].write_value(power_factor)
            await self.nodes['status'].write_value(status_str)
            await self.nodes['timestamp'].write_value(timestamp_str)

            return True

        except Exception as e:
            logger.error(f"Failed to send data to OPC-UA: {e}")
            return False

    async def run(self):
        """Main loop"""
        logger.info("Starting Energy Monitor")

        # Start PZEM reading first
        logger.info("Starting PZEM reader...")
        self.pzem_reader.start_reading()

        # Wait a moment for PZEM to get first reading
        await asyncio.sleep(3)

        # Connect to OPC-UA server
        if not await self.connect_opcua():
            logger.error("Cannot connect to OPC-UA server. Exiting.")
            return

        reading_count = 0

        try:
            while True:
                # Read PZEM data
                pzem_data = self.get_pzem_data()

                if pzem_data and 'voltage' in pzem_data:
                    # Send to OPC-UA server
                    if await self.send_data_to_opcua(pzem_data):
                        reading_count += 1

                        # Log every 5 readings
                        if reading_count % 5 == 0:
                            logger.info(f"Reading #{reading_count}: "
                                      f"V={pzem_data['voltage']:.1f}V, "
                                      f"I={pzem_data['current']:.3f}A, "
                                      f"P={pzem_data['power']:.0f}W")
                else:
                    logger.warning("No PZEM data available")

                # Wait for next reading
                await asyncio.sleep(SAMPLE_INTERVAL)

        except KeyboardInterrupt:
            logger.info("Stopping...")
        except Exception as e:
            logger.error(f"Unexpected error: {e}")
        finally:
            # Stop PZEM reading
            self.pzem_reader.running = False
            if self.opcua_client:
                await self.opcua_client.disconnect()
                logger.info("Disconnected from OPC-UA server")

async def main():
    monitor = EnergyMonitor()
    await monitor.run()

if __name__ == "__main__":
    asyncio.run(main())