#!/usr/bin/env python3
"""
Raspberry Pi PZEM OPC-UA Client
Reads PZEM data and sends to OPC-UA Server
Now with configurable settings via config file and environment variables
"""

import asyncio
import logging
import time
import threading
import serial
import struct
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional
from asyncua import Client
from logging.handlers import RotatingFileHandler
from dotenv import load_dotenv

class ConfigManager:
    """Manages configuration from both JSON file and environment variables"""
    
    def __init__(self, config_file: str = "config.json", env_file: str = ".env"):
        self.config_file = config_file
        self.env_file = env_file
        self.config = {}
        self.load_config()
    
    def load_config(self):
        """Load configuration from JSON file and environment variables"""
        # Load from JSON file first
        if Path(self.config_file).exists():
            try:
                with open(self.config_file, 'r') as f:
                    self.config = json.load(f)
                print(f"✅ Loaded config from {self.config_file}")
            except Exception as e:
                print(f"⚠️  Error loading config file: {e}")
                self.config = self._get_default_config()
        else:
            print(f"⚠️  Config file {self.config_file} not found, using defaults")
            self.config = self._get_default_config()
        
        # Load environment file
        if Path(self.env_file).exists():
            load_dotenv(self.env_file)
            print(f"✅ Loaded environment from {self.env_file}")
        
        # Override with environment variables
        self._apply_env_overrides()
    
    def _get_default_config(self) -> Dict[str, Any]:
        """Default configuration"""
        return {
            "application": {
                "name": "PZEM OPC-UA Client",
                "version": "1.0.0",
                "environment": "development"
            },
            "opcua": {
                "server_url": "opc.tcp://localhost:4840/UA",
                "connection": {
                    "timeout": 10,
                    "retry_attempts": 3,
                    "retry_delay": 5
                },
                "nodes": {
                    "voltage": "ns=1;s=VirtualEnergyMeter.Voltage",
                    "current": "ns=1;s=VirtualEnergyMeter.Current",
                    "power": "ns=1;s=VirtualEnergyMeter.Power",
                    "energy": "ns=1;s=VirtualEnergyMeter.Energy",
                    "frequency": "ns=1;s=VirtualEnergyMeter.Frequency",
                    "power_factor": "ns=1;s=VirtualEnergyMeter.PowerFactor",
                    "status": "ns=1;s=VirtualEnergyMeter.Status",
                    "timestamp": "ns=1;s=VirtualEnergyMeter.LastUpdate"
                }
            },
            "pzem": {
                "device": "/dev/ttyAMA0",
                "serial": {
                    "baudrate": 9600,
                    "timeout": 2
                },
                "protocol": {
                    "read_delay": 0.2
                }
            },
            "timing": {
                "sample_interval": 2.0,
                "startup_delay": 3,
                "pzem_read_interval": 2
            },
            "logging": {
                "level": "INFO",
                "format": "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
                "file": "energy_monitor.log",
                "enable_file_logging": False,
                "log_every_n_readings": 5
            }
        }
    
    def _apply_env_overrides(self):
        """Override config values with environment variables (only sensitive/environment-specific ones)"""
        env_mappings = {
            # Sensitive/Environment-specific settings only
            'OPCUA_SERVER_URL': ('opcua', 'server_url'),
            'OPCUA_USERNAME': ('opcua', 'username'),
            'OPCUA_PASSWORD': ('opcua', 'password'),
            'PZEM_DEVICE': ('pzem', 'device'),
            'LOG_LEVEL': ('logging', 'level'),
            'ENVIRONMENT': ('application', 'environment'),
            
            # Optional overrides for any config.json setting
            'SAMPLE_INTERVAL': ('timing', 'sample_interval', float),
            'ENABLE_FILE_LOGGING': ('logging', 'enable_file_logging', lambda x: x.lower() == 'true'),
            'LOG_EVERY_N_READINGS': ('logging', 'log_every_n_readings', int),
            'OPCUA_TIMEOUT': ('opcua', 'connection', 'timeout', int),
            'OPCUA_RETRY_ATTEMPTS': ('opcua', 'connection', 'retry_attempts', int),
            'PZEM_BAUDRATE': ('pzem', 'serial', 'baudrate', int),
            'PZEM_TIMEOUT': ('pzem', 'serial', 'timeout', int),
            'PZEM_READ_DELAY': ('pzem', 'protocol', 'read_delay', float),
        }
        
        for env_var, config_path in env_mappings.items():
            value = os.getenv(env_var)
            if value is not None:
                try:
                    # Handle type conversion
                    if len(config_path) > 2 and callable(config_path[2]):
                        try:
                            value = config_path[2](value)
                        except (ValueError, TypeError):
                            print(f"⚠️  Invalid value for {env_var}: {value}")
                            continue
                    
                    # Set nested config value safely
                    config_section = self.config
                    for i, key in enumerate(config_path[:-1]):
                        if key not in config_section:
                            config_section[key] = {}
                        elif not isinstance(config_section[key], dict):
                            # If the path exists but isn't a dict, create a new dict
                            print(f"⚠️  Overriding non-dict value at path: {'.'.join(config_path[:i+1])}")
                            config_section[key] = {}
                        config_section = config_section[key]
                    
                    # Set the final value
                    final_key = config_path[-1]
                    config_section[final_key] = value
                    print(f"🔧 Environment override: {env_var} = {value}")
                    
                except Exception as e:
                    print(f"❌ Error setting {env_var} to path {'.'.join(config_path)}: {e}")
                    print(f"   Current config section type: {type(config_section)}")
                    continue
    
    def get(self, *keys, default=None):
        """Get nested configuration value"""
        value = self.config
        try:
            for key in keys:
                value = value[key]
            return value
        except (KeyError, TypeError):
            return default


class PZEMReader:
    """PZEM reader with configurable settings"""

    def __init__(self, config: ConfigManager):
        self.config = config
        self.device = config.get('pzem', 'device', default='/dev/ttyAMA0')
        self.baudrate = config.get('pzem', 'serial', 'baudrate', default=9600)
        self.timeout = config.get('pzem', 'serial', 'timeout', default=2)
        self.read_delay = config.get('pzem', 'protocol', 'read_delay', default=0.2)
        self.read_interval = config.get('timing', 'pzem_read_interval', default=2)
        
        self.latest_data = {}
        self.running = False
        self.logger = logging.getLogger(self.__class__.__name__)

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
            ser = serial.Serial(self.device, self.baudrate, timeout=self.timeout)
            command = bytes([0x01, 0x04, 0x00, 0x00, 0x00, 0x0A, 0x70, 0x0D])
            ser.write(command)
            time.sleep(self.read_delay)
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
        self.logger.info(f"PZEM reading started on {self.device}")

    def _read_loop(self):
        while self.running:
            try:
                data = self.read_pzem_data()
                if isinstance(data, dict) and 'voltage' in data:
                    self.latest_data = data
                    self.logger.debug(f"PZEM: {data['voltage']:.1f}V, {data['current']:.3f}A, {data['power']:.1f}W")
                else:
                    self.latest_data = {"status": str(data)}
                    self.logger.warning(f"PZEM error: {data}")
            except Exception as e:
                self.logger.error(f"PZEM read error: {e}")
                self.latest_data = {"status": f"Error: {e}"}
            time.sleep(self.read_interval)

    def get_latest_data(self):
        return self.latest_data


class EnergyMonitor:
    def __init__(self, config: ConfigManager):
        self.config = config
        self.opcua_client = None
        self.pzem_reader = PZEMReader(config)
        self.nodes = {}
        self.logger = logging.getLogger(self.__class__.__name__)

    def get_pzem_data(self):
        """Get current PZEM readings"""
        return self.pzem_reader.get_latest_data()

    async def connect_opcua(self):
        """Connect to Windows OPC-UA server with retry logic"""
        server_url = self.config.get('opcua', 'server_url')
        retry_attempts = self.config.get('opcua', 'connection', 'retry_attempts', default=3)
        retry_delay = self.config.get('opcua', 'connection', 'retry_delay', default=5)
        
        for attempt in range(retry_attempts):
            try:
                self.logger.info(f"Connecting to OPC-UA server: {server_url} (attempt {attempt + 1})")
                self.opcua_client = Client(url=server_url)
                
                # Set authentication if provided
                username = self.config.get('opcua', 'username')
                password = self.config.get('opcua', 'password')
                if username and password:
                    self.opcua_client.set_user(username)
                    self.opcua_client.set_password(password)
                    self.logger.info(f"Using authentication for user: {username}")
                
                await self.opcua_client.connect()

                # Get node references
                node_ids = self.config.get('opcua', 'nodes', default={})
                for node_name, node_id in node_ids.items():
                    try:
                        self.nodes[node_name] = self.opcua_client.get_node(node_id)
                        self.logger.debug(f"Node '{node_name}' mapped to {node_id}")
                    except Exception as e:
                        self.logger.warning(f"Failed to map node '{node_name}': {e}")

                self.logger.info("✅ Connected to OPC-UA server")
                return True

            except Exception as e:
                self.logger.error(f"❌ OPC-UA connection attempt {attempt + 1} failed: {e}")
                if attempt < retry_attempts - 1:
                    self.logger.info(f"Retrying in {retry_delay} seconds...")
                    await asyncio.sleep(retry_delay)
        
        return False

    async def send_data_to_opcua(self, data):
        """Send PZEM data to OPC-UA server"""
        try:
            # Convert all values to proper types for OPC-UA
            values = {
                'voltage': float(data.get('voltage', 0.0)),
                'current': float(data.get('current', 0.0)),
                'power': float(data.get('power', 0.0)),
                'energy': float(data.get('energy', 0.0)),
                'frequency': float(data.get('frequency', 50.0)),
                'power_factor': float(data.get('power_factor', 1.0)),
                'status': str(data.get('status', 'OK')),
                'timestamp': datetime.now().isoformat()
            }

            # Send all data to OPC-UA nodes
            for key, value in values.items():
                if key in self.nodes:
                    try:
                        await self.nodes[key].write_value(value)
                    except Exception as e:
                        self.logger.warning(f"Failed to write {key}: {e}")
                        return False

            return True

        except Exception as e:
            self.logger.error(f"Failed to send data to OPC-UA: {e}")
            return False

    async def run(self):
        """Main loop"""
        self.logger.info("Starting Energy Monitor")
        self.logger.info(f"Application: {self.config.get('application', 'name')} v{self.config.get('application', 'version')}")
        self.logger.info(f"Environment: {self.config.get('application', 'environment')}")

        # Start PZEM reading first
        self.logger.info("Starting PZEM reader...")
        self.pzem_reader.start_reading()

        # Wait for PZEM to get first reading
        startup_delay = self.config.get('timing', 'startup_delay', default=3)
        self.logger.info(f"Waiting {startup_delay} seconds for PZEM initialization...")
        await asyncio.sleep(startup_delay)

        # Connect to OPC-UA server
        if not await self.connect_opcua():
            self.logger.error("Cannot connect to OPC-UA server. Exiting.")
            return

        reading_count = 0
        sample_interval = self.config.get('timing', 'sample_interval', default=2.0)
        log_every_n = self.config.get('logging', 'log_every_n_readings', default=5)
        
        self.logger.info(f"Starting main loop with {sample_interval}s interval")

        try:
            while True:
                # Read PZEM data
                pzem_data = self.get_pzem_data()

                if pzem_data and 'voltage' in pzem_data:
                    # Send to OPC-UA server
                    if await self.send_data_to_opcua(pzem_data):
                        reading_count += 1

                        # Log every N readings
                        if reading_count % log_every_n == 0:
                            self.logger.info(f"Reading #{reading_count}: "
                                          f"V={pzem_data['voltage']:.1f}V, "
                                          f"I={pzem_data['current']:.3f}A, "
                                          f"P={pzem_data['power']:.0f}W, "
                                          f"E={pzem_data['energy']:.0f}Wh, "
                                          f"F={pzem_data['frequency']:.1f}Hz, "
                                          f"PF={pzem_data['power_factor']:.2f}")
                    else:
                        self.logger.warning("Failed to send data to OPC-UA server")
                else:
                    self.logger.warning(f"No valid PZEM data available: {pzem_data}")

                # Wait for next reading
                await asyncio.sleep(sample_interval)

        except KeyboardInterrupt:
            self.logger.info("Stopping due to keyboard interrupt...")
        except Exception as e:
            self.logger.error(f"Unexpected error in main loop: {e}")
            raise
        finally:
            # Stop PZEM reading
            self.logger.info("Shutting down...")
            self.pzem_reader.running = False
            if self.opcua_client:
                try:
                    await self.opcua_client.disconnect()
                    self.logger.info("Disconnected from OPC-UA server")
                except Exception as e:
                    self.logger.warning(f"Error disconnecting from OPC-UA: {e}")


def setup_logging(config: ConfigManager):
    """Setup logging configuration"""
    log_level = getattr(logging, config.get('logging', 'level', default='INFO').upper())
    log_format = config.get('logging', 'format', default='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    
    # Create logger
    logger = logging.getLogger()
    logger.setLevel(log_level)
    
    # Clear existing handlers
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
    
    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(log_level)
    formatter = logging.Formatter(log_format)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    
    # File handler (if enabled)
    if config.get('logging', 'enable_file_logging', default=False):
        log_file = config.get('logging', 'file', default='energy_monitor.log')
        try:
            file_handler = RotatingFileHandler(
                log_file,
                maxBytes=10*1024*1024,  # 10MB
                backupCount=3
            )
            file_handler.setLevel(log_level)
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)
            print(f"📄 File logging enabled: {log_file}")
        except Exception as e:
            print(f"⚠️  Could not enable file logging: {e}")


def print_startup_info(config: ConfigManager):
    """Print startup information"""
    print("\n" + "="*60)
    print(f"🚀 {config.get('application', 'name')} v{config.get('application', 'version')}")
    print(f"🌍 Environment: {config.get('application', 'environment')}")
    print(f"🔌 OPC-UA Server: {config.get('opcua', 'server_url')}")
    print(f"⚡ PZEM Device: {config.get('pzem', 'device')}")
    print(f"⏱️  Sample Interval: {config.get('timing', 'sample_interval')}s")
    print(f"📊 Log Level: {config.get('logging', 'level')}")
    print("="*60 + "\n")


async def main():
    try:
        # Load configuration
        print("🔧 Loading configuration...")
        config = ConfigManager()
        
        # Print startup info
        print_startup_info(config)
        
        # Setup logging
        setup_logging(config)
        
        # Start monitor
        monitor = EnergyMonitor(config)
        await monitor.run()
        
    except KeyboardInterrupt:
        print("\n👋 Goodbye!")
    except Exception as e:
        print(f"\n💥 Fatal error: {e}")
        raise


if __name__ == "__main__":
    asyncio.run(main())