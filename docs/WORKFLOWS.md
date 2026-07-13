# MyDolphin Plus Integration Workflows

This document describes the key workflows in the MyDolphin Plus Home Assistant integration, including startup initialization and various update mechanisms.

> **Note**: This document contains Mermaid sequence diagrams. To view them properly:
>
> - On GitHub: Diagrams render automatically
> - In VS Code/Cursor: Install the "Markdown Preview Mermaid Support" extension
> - In other editors: Use a Mermaid-compatible Markdown viewer or preview on GitHub

## Table of Contents

- [Startup Flow](#startup-flow)
- [Recovery Flow](#recovery-flow)
- [Update Flows](#update-flows)
  - [Flow 1: Periodic API Update (REST)](#flow-1-periodic-api-update-rest)
  - [Flow 2: Periodic MQTT Update (Device Shadow Request)](#flow-2-periodic-mqtt-update-device-shadow-request)
  - [Flow 3: Real-time MQTT Push Updates](#flow-3-real-time-mqtt-push-updates)
  - [Flow 4: Entity Data Request & User Actions](#flow-4-entity-data-request--user-actions)

---

## Startup Flow

The startup flow describes how the integration initializes when Home Assistant loads it. This happens during HA startup or when the integration is added/reloaded.

### Overview

The integration follows these main steps:

1. **Configuration Loading**: Decrypt stored credentials and load configuration
2. **Component Initialization**: Create coordinator, API client, and AWS client instances
3. **Authentication**: Login to MyDolphin API and obtain tokens
4. **AWS IoT Connection**: Connect to AWS IoT MQTT broker
5. **Initial State Fetch**: Subscribe to topics and fetch initial device state

### Sequence Diagram

```mermaid
sequenceDiagram
    participant HA as Home Assistant
    participant Init as __init__.py
    participant PM as PasswordManager
    participant CM as ConfigManager
    participant Coord as Coordinator
    participant API as RestAPI
    participant AWS as AWSClient
    participant Broker as AWS IoT Broker

    HA->>Init: async_setup_entry(hass, entry)
    Init->>PM: decrypt(hass, entry_config, entry_id)
    PM-->>Init: Decrypted credentials

    Init->>CM: __init__(hass, entry)
    Init->>CM: initialize(entry_config)
    CM->>CM: _load() - Load from storage
    CM->>CM: Load translations
    CM-->>Init: is_initialized

    alt Initialization successful
        Init->>Coord: __init__(hass, config_manager)
        Coord->>API: __init__(hass, config_manager)
        Coord->>AWS: __init__(hass, config_manager)
        Coord->>Coord: _load_signal_handlers()

        Init->>Init: Store coordinator in hass.data

        alt HA already running
            Init->>Coord: initialize()
        else HA not running yet
            Init->>HA: Listen for EVENT_HOMEASSISTANT_START
        end

        Coord->>Coord: _build_data_mapping()
        Coord->>HA: async_forward_entry_setups(PLATFORMS)
        Coord->>Coord: async_request_refresh()
        Coord->>API: initialize()

        API->>API: _initialize_session()
        API->>API: _login()

        alt Has cached API token
            API->>API: _set_status(TEMPORARY_CONNECTED)
            alt Missing motor_unit_serial
                API->>API: _set_actual_motor_unit_serial()
                API->>API: POST robot details by SN
                API->>CM: update_motor_unit_serial()
            end
        else No cached token
            API->>API: _email_validation()
            API->>API: _service_login()
            API->>API: POST login
            API->>CM: update_login_details(api_token, serial_number)
            API->>API: _set_actual_motor_unit_serial()
        end

        API->>API: _generate_aws_token()

        alt Has cached AWS credentials & valid
            API->>API: Use cached credentials
        else Needs fresh credentials
            API->>API: _get_aws_token() - Encrypt motor_unit_serial
            API->>API: POST token endpoint
            API->>CM: update_last_token_fetch()
            API->>CM: update_aws_credentials_expiry()
        end

        API->>API: _set_status(CONNECTED)
        API->>Coord: Signal API_STATUS → CONNECTED

        Coord->>API: update()
        API->>API: _load_details()
        Coord->>AWS: update_api_data(api_data)
        Coord->>AWS: initialize()

        AWS->>AWS: Create mqtt_connection_builder
        AWS->>AWS: _awsiot_client.connect()
        AWS->>Broker: Connect to AWS IoT
        Broker-->>AWS: Connection established

        AWS->>AWS: _on_connection_success()
        AWS->>AWS: _set_status(CONNECTED)
        AWS->>AWS: Subscribe to topics
        AWS->>Broker: Subscribe to get/accepted
        AWS->>Broker: Subscribe to update/accepted
        AWS->>Broker: Subscribe to update/rejected
        AWS->>Broker: Subscribe to update/documents

        AWS->>Broker: Publish to get topic
        Broker-->>AWS: Initial device state
        AWS->>AWS: _on_message_received()
        AWS->>AWS: Update data dictionary

        AWS->>Coord: Signal AWS_CLIENT_STATUS → CONNECTED
        Coord->>AWS: update()
    end

    Init-->>HA: return initialized
```

### Key Components

- **`__init__.py`**: Entry point, orchestrates the setup
- **`PasswordManager`**: Handles encrypted credential storage
- **`ConfigManager`**: Manages configuration and persistent storage
- **`Coordinator`**: Central coordinator for data updates and entity management
- **`RestAPI`**: Handles REST API communication with MyDolphin service
- **`AWSClient`**: Manages AWS IoT MQTT connection and real-time updates

### Authentication Flow

1. **API Token**: Obtained via email/password login to MyDolphin API
2. **Motor Unit Serial**: Retrieved using the API token
3. **AWS Token**: Generated by encrypting the motor unit serial
4. **AWS IoT Credentials**: Temporary IAM credentials obtained using the AWS token
5. **MQTT Connection**: Uses IAM credentials to connect to AWS IoT Core

---

## Recovery Flow

The recovery flow handles connection failures and implements automatic reconnection with exponential backoff. This ensures the integration can recover from temporary network issues, expired credentials, or service disruptions without manual intervention.

### Overview

When connections fail, the integration:

1. **Detects the failure** through status changes or connection callbacks
2. **Cleans up resources** by terminating AWS client connections
3. **Implements exponential backoff** to avoid overwhelming services during outages
4. **Attempts reconnection** by re-initializing the API client
5. **Resets state flags** to ensure fresh data fetch after recovery

### Recovery Scenarios

#### 1. API Connection Failures

- **Network errors**: Timeout, connection refused, DNS failures
- **Authentication issues**: Invalid credentials, expired tokens (401 errors)
- **Service issues**: API not found (404), server errors (500+)
- **Token expiration**: AWS IoT credentials expiring after TTL

#### 2. AWS IoT MQTT Failures

- **Connection interrupted**: Network drops, firewall changes
- **Connection closed**: Broker termination, credential expiration
- **Connection failure**: Initial connection attempt fails

#### 3. Coordinator Response

- **Status monitoring**: Listens to `SIGNAL_API_STATUS` and `SIGNAL_AWS_CLIENT_STATUS`
- **Exponential backoff**: 1min → 2min → 4min → 8min → 15min (max)
- **Resource cleanup**: Terminates AWS client before retry
- **Re-initialization**: Full API login and AWS connection cycle

### Sequence Diagram - API Disconnection & Recovery

```mermaid
sequenceDiagram
    participant API as RestAPI
    participant Coord as Coordinator
    participant AWS as AWSClient
    participant CM as ConfigManager

    Note over API,CM: API Connection Failure Scenario

    API->>API: HTTP request fails (network/auth/server error)
    API->>API: _handle_client_error() or _handle_server_timeout()

    alt Token expired (401) and old token
        API->>CM: reset_login_details()
        CM->>CM: Clear API token, AWS token
        API->>API: _set_status(EXPIRED_TOKEN, message)
    else Other failures
        API->>API: _set_status(FAILED, message)
    end

    Note over API: Status change detected

    alt Status is disconnected
        API->>API: status.is_disconnected() == True
        API->>API: _device_loaded = False
        Note over API: Flag reset ensures _load_details()<br/>runs on next connection
    end

    API->>Coord: dispatcher_send(SIGNAL_API_STATUS, status)

    Coord->>Coord: _on_api_status_changed(entry_id, status)

    alt Status in [FAILED, INVALID_CREDENTIALS, EXPIRED_TOKEN]
        Coord->>Coord: _handle_connection_failure()

        Coord->>AWS: terminate()
        AWS->>AWS: disconnect()
        AWS->>AWS: _set_status(DISCONNECTED)

        Coord->>Coord: Calculate exponential backoff
        Note over Coord: backoff = min(2^attempts, 15 minutes)<br/>attempts: 0→1min, 1→2min, 2→4min,<br/>3→8min, 4+→15min

        Coord->>Coord: Increment _reconnection_attempts
        Coord->>Coord: Log warning with wait time
        Coord->>Coord: sleep(backoff_interval)

        Note over Coord: After backoff period

        Coord->>API: initialize()
        API->>API: _initialize_session()
        API->>API: _login()

        alt Has cached API token
            API->>API: _set_status(TEMPORARY_CONNECTED)
            API->>API: _generate_aws_token()
        else No token
            API->>API: _service_login()
            API->>API: POST login
            API->>CM: update_login_details()
            API->>API: _generate_aws_token()
        end

        alt Token generation successful
            API->>API: _set_status(CONNECTED)
            API->>Coord: dispatcher_send(SIGNAL_API_STATUS, CONNECTED)

            Coord->>Coord: _on_api_status_changed(CONNECTED)
            Coord->>Coord: _reconnection_attempts = 0
            Note over Coord: Reset backoff counter on success

            Coord->>API: update()
            API->>API: Check _device_loaded == False
            API->>API: _load_details() executes
            API->>API: Set _device_loaded = True

            Coord->>AWS: update_api_data(api_data)
            Coord->>AWS: initialize()

            Note over AWS: Full MQTT reconnection
        else Token generation failed
            Note over API: Cycle repeats with next backoff
        end
    end
```

### Sequence Diagram - AWS IoT Disconnection & Recovery

```mermaid
sequenceDiagram
    participant Broker as AWS IoT Broker
    participant AWS as AWSClient
    participant Coord as Coordinator
    participant API as RestAPI

    Note over Broker,API: AWS IoT Connection Failure Scenario

    alt Connection Interrupted
        Broker->>AWS: Connection interrupted callback
        AWS->>AWS: _on_connection_interrupted(error)
        AWS->>AWS: Log warning with error code
        Note over AWS: Automatic reconnection<br/>handled by AWS SDK

    else Connection Closed
        Broker->>AWS: Connection closed callback
        AWS->>AWS: _on_connection_closed()
        AWS->>AWS: Log info about closure

    else Connection Failure
        Broker--xAWS: Connection attempt fails
        AWS->>AWS: _on_connection_failure(error)
        AWS->>AWS: Log error with details
        AWS->>AWS: _set_status(FAILED, message)
    end

    AWS->>Coord: dispatcher_send(SIGNAL_AWS_CLIENT_STATUS, status)

    Coord->>Coord: _on_aws_client_status_changed(entry_id, status)

    alt Status in [FAILED, NOT_CONNECTED]
        Coord->>Coord: _handle_connection_failure()

        Coord->>AWS: terminate()
        AWS->>AWS: Cleanup and disconnect
        AWS->>AWS: _set_status(DISCONNECTED)

        Coord->>Coord: Calculate exponential backoff
        Coord->>Coord: Increment _reconnection_attempts
        Coord->>Coord: sleep(backoff_interval)

        Note over Coord: After backoff period

        Coord->>API: initialize()
        Note over API: Full API re-initialization<br/>to refresh AWS credentials

        API->>API: _login()
        API->>API: _generate_aws_token()

        alt AWS token refresh successful
            API->>API: Update AWS credentials in data
            API->>API: _set_status(CONNECTED)
            API->>Coord: dispatcher_send(SIGNAL_API_STATUS, CONNECTED)

            Coord->>Coord: _on_api_status_changed(CONNECTED)
            Coord->>Coord: _reconnection_attempts = 0

            Coord->>AWS: update_api_data(api_data)
            AWS->>AWS: Extract new AWS credentials

            Coord->>AWS: initialize()
            AWS->>AWS: Create new mqtt_connection_builder
            AWS->>AWS: _awsiot_client.connect()
            AWS->>Broker: Connect with fresh credentials

            alt Connection successful
                Broker-->>AWS: Connection established
                AWS->>AWS: _on_connection_success()
                AWS->>AWS: _set_status(CONNECTED)
                AWS->>AWS: Subscribe to topics
                AWS->>Broker: Request device shadow

                AWS->>Coord: dispatcher_send(SIGNAL_AWS_CLIENT_STATUS, CONNECTED)
                Coord->>Coord: _on_aws_client_status_changed(CONNECTED)
                Coord->>Coord: _reconnection_attempts = 0
                Coord->>AWS: update()

                Note over AWS,Coord: System fully recovered
            else Connection failed
                Note over AWS: Retry with next backoff
            end
        end
    end

    alt Connection Resumed (after interruption)
        Broker->>AWS: Connection resumed callback
        AWS->>AWS: _on_connection_resumed()
        AWS->>AWS: Log info about resume
        AWS->>AWS: Resubscribe to topics
        AWS->>Broker: Request current device shadow
        Note over AWS: Automatic recovery complete
    end
```

### Exponential Backoff Strategy

The integration uses exponential backoff to handle reconnection attempts gracefully:

| Attempt | Backoff Time | Calculation  |
| ------- | ------------ | ------------ |
| 1st     | 1 minute     | 2^0 = 1      |
| 2nd     | 2 minutes    | 2^1 = 2      |
| 3rd     | 4 minutes    | 2^2 = 4      |
| 4th     | 8 minutes    | 2^3 = 8      |
| 5th+    | 15 minutes   | max(2^n, 15) |

**Benefits**:

- Prevents overwhelming services during outages
- Reduces unnecessary API calls during extended failures
- Allows time for transient issues to resolve
- Caps maximum wait time at 15 minutes

### Key Recovery Mechanisms

#### 1. Flag Reset on Disconnection

```python
# In RestAPI._set_status()
if status.is_disconnected():
    self._device_loaded = False
```

When API status becomes disconnected, the `_device_loaded` flag resets to `False`, ensuring that device details are fetched fresh on reconnection.

#### 2. Coordinator Status Listeners

```python
# Registered in Coordinator._load_signal_handlers()
SIGNAL_API_STATUS -> _on_api_status_changed()
SIGNAL_AWS_CLIENT_STATUS -> _on_aws_client_status_changed()
```

The coordinator monitors both API and AWS client status changes to trigger appropriate recovery actions.

#### 3. AWS Client Termination

```python
# In Coordinator._handle_connection_failure()
await self._aws_client.terminate()
```

Before attempting reconnection, the AWS client is properly terminated to clean up resources and reset connection state.

#### 4. Full Re-initialization

```python
# In Coordinator._handle_connection_failure()
await self._api.initialize()
```

Recovery triggers a full API re-initialization, which includes:

- Re-authentication (if needed)
- Fresh AWS IoT credentials
- New MQTT connection
- Topic re-subscription
- Initial state fetch

### Recovery Success Indicators

When recovery is successful:

1. ✅ `_reconnection_attempts` counter resets to 0
2. ✅ API status becomes `CONNECTED`
3. ✅ AWS client status becomes `CONNECTED`
4. ✅ `_device_loaded` flag is `False`, triggering fresh data fetch
5. ✅ Device shadow is requested and received
6. ✅ Entities update with current state
7. ✅ Normal operation resumes

### Failure Persistence

If reconnection continues to fail:

- Backoff time increases up to 15-minute maximum
- System keeps retrying indefinitely
- Home Assistant logs warnings with attempt numbers
- Entities show unavailable state
- User can manually reload integration to force immediate retry

---

## Update Flows

The integration uses four distinct update mechanisms to keep device state synchronized:

1. **Periodic API Update (REST)**: Less frequent updates of static data
2. **Periodic MQTT Update**: Regular polling of device shadow
3. **Real-time MQTT Push**: Async updates pushed from the cloud/device
4. **Entity Data Request**: On-demand data retrieval and user actions

---

## Flow 1: Periodic API Update (REST)

Fetches robot details from the MyDolphin REST API. This retrieves relatively static information like product description and firmware versions.

**Important**: The `_load_details()` method only executes **once per connection session**. After the initial fetch following a connection, it won't run again until the API disconnects and reconnects. This optimization reduces unnecessary API calls since device details rarely change.

### Sequence Diagram

```mermaid
sequenceDiagram
    participant Timer as HA Timer
    participant Coord as Coordinator
    participant API as RestAPI
    participant Server as MyDolphin API Server
    participant CM as ConfigManager

    Note over Timer,CM: Triggered by Coordinator's _async_update_data()

    Timer->>Coord: Periodic timer fires
    Coord->>Coord: _async_update_data()
    Coord->>Coord: Check api_connected & aws_client_connected

    Coord->>Coord: now - last_update_api >= UPDATE_API_INTERVAL?

    alt Time for API update (5 minutes default)
        Coord->>API: update()
        API->>API: Check status == CONNECTED
        API->>API: Check if _device_loaded == False

        alt Device not yet loaded
            API->>API: _load_details()

            API->>CM: Get api_token
            API->>CM: Get motor_unit_serial
            API->>API: Build headers with token
            API->>API: Build request_data with motor_unit_serial

            API->>Server: POST /robot_details
            Note right of Server: Headers: token<br/>Body: motor_unit_serial

            Server-->>API: Response {status, data, alert}

            alt Response status == SUCCESS
                API->>API: Extract data from payload
                loop For each key in DATA_ROBOT_DETAILS
                    API->>API: self.data[new_key] = data.get(key)
                end
                Note over API: Updates: Product Description,<br/>versions, etc.
            else Response status == FAILURE
                API->>API: Log error with alert message
            end

            API->>API: Set _device_loaded = True
            API->>Coord: Send SIGNAL_DEVICE_NEW
        else Already loaded
            Note over API: Skip _load_details()<br/>Use cached data
        end

        API-->>Coord: Robot details available in api_data
        Coord->>Coord: Update last_update_api = now
    else Not time yet
        Coord->>Coord: Skip API update
    end
```

### Timing

- **Check Interval**: The coordinator checks every 5 minutes (`UPDATE_API_INTERVAL`) if it's time to call `update()`
- **Actual Execution**: `_load_details()` only runs **once per connection**, not every 5 minutes
- **First Run**: Immediately after successful API connection on the first coordinator update cycle
- **Configurable**: Interval can be adjusted via constants in `consts.py`

### Data Retrieved

- Product Description
- Hardware Version
- Software Version
- PWS (Power Supply) Version
- Other static robot details

### Behavior

The `update()` method checks the `_device_loaded` flag to determine whether to fetch device details:

- **First Connection**: On the first `update()` call after successful API connection, `_device_loaded` is `False`, so `_load_details()` executes and fetches robot details from the API server
- **Subsequent Updates**: Flag is set to `True`, so `_load_details()` is skipped (details are cached in `api_data`)
- **After Disconnection**: When the API status transitions to any disconnected state (via `_set_status()`), the `_device_loaded` flag is automatically reset to `False`
  - Disconnected states: `DISCONNECTED`, `FAILED`, `INVALID_CREDENTIALS`, `EXPIRED_TOKEN`, `API_NOT_FOUND`, `NOT_CONNECTED`
- **Next Reconnection**: `_load_details()` will execute once again on the first `update()` call

**Rationale**: Device details (product info, firmware versions) are static and don't change during a connection session. This optimization significantly reduces API calls while ensuring fresh data is retrieved after any reconnection (which might indicate a firmware update or device replacement).

---

## Flow 2: Periodic MQTT Update (Device Shadow Request)

Requests the full device shadow state from AWS IoT at regular intervals (default: 30 seconds). This ensures the integration has the latest state even if push notifications are missed.

### Sequence Diagram

```mermaid
sequenceDiagram
    participant Timer as HA Timer
    participant Coord as Coordinator
    participant AWS as AWSClient
    participant Broker as AWS IoT Broker

    Note over Timer,Broker: Triggered by Coordinator's _async_update_data()

    Timer->>Coord: Periodic timer fires
    Coord->>Coord: _async_update_data()
    Coord->>Coord: Check api_connected & aws_client_connected

    Coord->>Coord: now - last_update_ws >= UPDATE_WS_INTERVAL?

    alt Time for WS update (30 seconds default)
        Coord->>AWS: update()
        AWS->>AWS: Check status == CONNECTED
        AWS->>AWS: _publish(topic_data.get, {})

        AWS->>AWS: Build empty payload {}
        AWS->>AWS: json.dumps(data)
        AWS->>Broker: Publish to $aws/things/{motor_unit_serial}/shadow/get
        Note right of Broker: QoS: AT_MOST_ONCE

        AWS->>AWS: log "Publishing #packet_id to topic"
        AWS->>AWS: partial(_on_publish_completed, packet_id, topic, payload)

        Note over AWS: QoS 0 — future resolves on local socket write, no broker ACK
        AWS->>AWS: _on_publish_completed(future, packet_id=…, topic=…, payload=…)
        AWS->>AWS: future.result() guarded by try/except
        AWS->>AWS: log "MQTT publish #packet_id completed" (or _LOGGER.exception on failure)

        Note over Broker: IoT Service processes request
        Broker->>AWS: Message on shadow/get/accepted
        AWS->>AWS: _on_message_received(topic, payload)
        AWS->>AWS: Parse JSON payload

        AWS->>AWS: Extract version & timestamp
        AWS->>AWS: Calculate diff = now - server_timestamp
        AWS->>AWS: Update WS_DATA_VERSION, WS_DATA_TIMESTAMP, WS_DATA_DIFF

        AWS->>AWS: Extract state.reported
        loop For each category in reported
            AWS->>AWS: category_data = reported.get(category)
            alt Category already exists in data
                AWS->>AWS: self.data[category].update(category_data)
            else New category
                AWS->>AWS: self.data[category] = category_data
            end
        end

        Note over AWS: Updated categories: systemState,<br/>cycleInfo, led, wifi, debug,<br/>filterBagIndication, robotError, pwsError

        alt Robot family == M700 and topic == get_accepted
            AWS->>AWS: _read_temperature_and_in_water_details()
            Note over AWS: Special handling for M700 models
        end

        AWS-->>Coord: AWS data updated
        Coord->>Coord: Update last_update_ws = now
    else Not time yet
        Coord->>Coord: Skip WS update
    end

    Coord->>Coord: _set_system_status_details()
    Coord->>AWS: Access aws_data
    Coord->>Coord: SystemDetails.update(aws_data)
    Coord->>Coord: Calculate vacuum_state, robot_state, power_unit_state
```

### Interval

- **Default**: 30 seconds (`UPDATE_WS_INTERVAL`)
- **Configurable**: Can be adjusted via constants

### Data Retrieved

- **System State**: Power supply state, robot state, turn-on count
- **Cycle Info**: Cleaning mode, cycle duration, start time
- **LED Settings**: Enable state, mode, intensity
- **WiFi Info**: Network name, RSSI
- **Debug Info**: WiFi signal strength, diagnostics
- **Filter Status**: Filter bag indication state
- **Errors**: Robot errors, power supply errors

### AWS IoT Topics

- **Request**: `$aws/things/{motor_unit_serial}/shadow/get`
- **Response**: `$aws/things/{motor_unit_serial}/shadow/get/accepted`

---

## Flow 3: Real-time MQTT Push Updates

Handles asynchronous messages pushed from AWS IoT when the device state changes. This provides real-time responsiveness without polling.

### Sequence Diagram

```mermaid
sequenceDiagram
    participant Broker as AWS IoT Broker
    participant MQTT as MQTT Connection
    participant AWS as AWSClient
    participant Coord as Coordinator

    Note over Broker,Coord: Async push updates from device/cloud

    alt Update Type: Dynamic Content
        Broker->>MQTT: Message on shadow/update/documents
        MQTT->>AWS: Callback: _on_message_received(topic, payload)
        AWS->>AWS: Parse JSON payload
        AWS->>AWS: Extract dynamic content

        AWS->>AWS: _on_dynamic_content_received(payload_data)
        AWS->>AWS: message_type = message.get(DYNAMIC_TYPE)
        AWS->>AWS: content = message.get(DYNAMIC_CONTENT)

        alt Message type == DYNAMIC_TYPE_PWS_REQUEST
            AWS->>AWS: _on_pws_request_message(message)
            AWS->>AWS: Extract direction & remote_control_mode

            alt Has direction (joystick command)
                AWS->>AWS: data[DATA_SECTION_ACTIVITY] = direction
                Note over AWS: Updates joystick direction:<br/>forward, backward, left, right
            end

            alt remote_control_mode == EXIT
                AWS->>AWS: data[DATA_SECTION_ACTIVITY] = None
                Note over AWS: Exits joystick mode
            end
        end

        AWS->>AWS: Store in data[DATA_SECTION_DYNAMIC][message_type]
        Note over AWS: Also handles temperature,<br/>IOT_RESPONSE types
    end

    alt Update Type: Device Shadow Accepted
        Broker->>MQTT: Message on shadow/update/accepted
        MQTT->>AWS: Callback: _on_message_received(topic, payload)
        AWS->>AWS: Parse JSON payload

        AWS->>AWS: Extract version, timestamp, state
        AWS->>AWS: reported = state.get(DATA_STATE_REPORTED, {})

        loop For each category in reported
            AWS->>AWS: Merge category_data into self.data[category]
        end

        AWS->>AWS: desired = state.get(DATA_STATE_DESIRED)

        alt Has desired state
            AWS->>AWS: Extract cleaning_mode from desired
            AWS->>AWS: mode = cleaning_mode.get(CONF_MODE)

            alt Mode is not None
                AWS->>AWS: sleep(1)
                AWS->>AWS: _set_cycle_time(mode)
                AWS->>Coord: Get clean_cycle_time for mode
                AWS->>AWS: Build payload with duration
                AWS->>AWS: _send_desired_command(payload)
                AWS->>Broker: Publish cycle time to update topic
                Note over AWS: Syncs configured cycle time<br/>with device
            end
        end
    end

    alt Update Type: Rejected
        Broker->>MQTT: Message on shadow/update/rejected
        MQTT->>AWS: Callback: _on_message_received(topic, payload)
        AWS->>AWS: Parse JSON payload
        AWS->>AWS: Log error with payload details
        Note over AWS: Indicates command was rejected<br/>by device or IoT service
    end

    Note over AWS,Coord: Data now available for entities

    AWS->>Coord: _on_data_update_callback() triggered
    Coord->>Coord: _on_mqtt_data_update()
    Coord->>Coord: Check time_since_last_refresh

    alt Max delay exceeded (5 seconds)
        Coord->>Coord: Force immediate refresh
        Coord->>Coord: async_request_refresh()
    else Within cooldown window
        Coord->>Coord: _mqtt_debouncer.async_call()
        Note over Coord: Debounced refresh with 1s cooldown
        Coord->>Coord: _debounced_mqtt_refresh() after delay
        Coord->>Coord: async_request_refresh()
    end

    AWS->>Coord: Data automatically available via aws_data property
```

### MQTT Update Debouncing

To optimize performance and prevent excessive coordinator refreshes, MQTT updates are debounced:

- **Debounce Cooldown**: 1.0 second - Multiple rapid updates are batched together
- **Maximum Delay Safety Net**: 5.0 seconds - Forces immediate refresh if no update occurred for too long
- **Callback Mechanism**: AWSClient triggers `_on_mqtt_data_update()` callback after processing each message
- **Coordinator Response**: Debouncer ensures coordinator refresh happens at most once per second, even with multiple rapid MQTT messages

**Benefits**:

- Reduces CPU usage by batching rapid updates
- Prevents UI flicker from excessive entity updates
- Ensures responsiveness with safety net for missed updates
- Optimizes Home Assistant entity refresh cycles

### Message Types

#### 1. Dynamic Content (`shadow/update/documents`)

- **Joystick Control**: Real-time direction updates during manual control
- **Temperature**: Water temperature readings (M700 models)
- **IoT Responses**: Various device responses and status updates

#### 2. Shadow Updates (`shadow/update/accepted`)

- **State Changes**: Robot status, cleaning mode, power supply state
- **Configuration Sync**: Cycle time synchronization after mode changes
- **Sensor Updates**: Filter status, LED state, WiFi info

#### 3. Error Messages (`shadow/update/rejected`)

- **Command Failures**: When a command cannot be executed
- **Validation Errors**: Invalid command parameters

### AWS IoT Topics

- **Dynamic**: `$aws/things/{motor_unit_serial}/shadow/update/documents`
- **Update Accepted**: `$aws/things/{motor_unit_serial}/shadow/update/accepted`
- **Update Rejected**: `$aws/things/{motor_unit_serial}/shadow/update/rejected`

---

## Flow 4: Entity Data Request & User Actions

Handles on-demand entity state requests and user-initiated actions (like starting the vacuum or changing LED settings).

### Sequence Diagram

```mermaid
sequenceDiagram
    participant User as User/Automation
    participant Entity as Entity (Sensor/Vacuum)
    participant Coord as Coordinator
    participant AWS as AWSClient
    participant API as RestAPI
    participant Broker as AWS IoT Broker

    Note over User,Broker: Flow A: Entity State Request

    Entity->>Coord: get_data(entity_description)
    Coord->>Coord: handler = _data_mapping.get(entity_description.key)
    Coord->>Coord: Check _system_details.is_updated

    alt Handler found & system updated
        Coord->>Coord: Execute handler (e.g., _get_vacuum_data)

        Coord->>AWS: Access self.aws_data
        Note over Coord: Gets: cycle_info, cleaning_mode,<br/>led, wifi, systemState, etc.

        Coord->>API: Access self.api_data
        Note over Coord: Gets: Product Description,<br/>versions, robot name

        Coord->>Coord: Build result dict
        Note over Coord: {<br/>  ATTR_STATE: state,<br/>  ATTR_ATTRIBUTES: {...},<br/>  ATTR_ACTIONS: {<br/>    SERVICE_START: _vacuum_start,<br/>    SERVICE_PAUSE: _vacuum_pause,<br/>    ...<br/>  }<br/>}

        Coord-->>Entity: Return {state, attributes, actions}
        Entity->>Entity: Update entity state in HA
    else Handler not found
        Coord->>Coord: Log error
        Coord-->>Entity: Return None
    end

    Note over User,Broker: Flow B: User Action - Control Commands

    User->>Entity: Call service (e.g., vacuum.start)
    Entity->>Coord: get_device_action(entity_description, action_key)
    Coord->>Coord: device_data = get_data(entity_description)
    Coord->>Coord: actions = device_data.get(ATTR_ACTIONS)
    Coord->>Coord: async_action = actions.get(action_key)
    Coord-->>Entity: Return async_action callable

    Entity->>Coord: Call async_action (e.g., _vacuum_start)
    Coord->>Coord: Get current mode from _get_vacuum_data
    Coord->>AWS: set_cleaning_mode(mode)

    AWS->>AWS: _send_desired_command(payload)
    AWS->>AWS: data = {state: {desired: payload}}
    AWS->>AWS: _publish(topic_data.update, data)

    AWS->>AWS: Build JSON payload
    AWS->>Broker: Publish to shadow/update
    Note right of Broker: {<br/>  "state": {<br/>    "desired": {<br/>      "scheduleData": {<br/>        "cleaningMode": {<br/>          "mode": "regular"<br/>        }<br/>      }<br/>    }<br/>  }<br/>}

    Broker-->>AWS: Publish acknowledged
    AWS->>AWS: _on_publish_completed()

    Broker->>Broker: Process desired state
    Broker->>Broker: Apply to device shadow
    Broker->>AWS: Push update/accepted
    AWS->>AWS: _on_message_received()
    AWS->>AWS: Update local data with new state

    Note over Entity: Next entity refresh will show updated state

    Note over User,Broker: Flow C: Other Action Examples

    User->>Entity: light.turn_on (LED)
    Entity->>Coord: _set_led_enabled()
    Coord->>AWS: set_led_enabled(True)
    AWS->>AWS: _send_desired_command({led: {ledEnable: 1}})
    AWS->>Broker: Publish to shadow/update

    User->>Entity: remote.send_command (Joystick)
    Entity->>Coord: _set_joystick_mode(direction)
    Coord->>AWS: set_joystick_mode(direction)
    AWS->>AWS: _send_dynamic_command(JOYSTICK, payload)
    AWS->>Broker: Publish to shadow/update/documents
    Note right of Broker: {<br/>  "dynamicType": "pwsRequest",<br/>  "description": "joystick",<br/>  "content": {<br/>    "direction": "forward",<br/>    "speed": 60<br/>  }<br/>}
```

### Entity State Retrieval

The coordinator maintains a mapping of entity descriptions to data handler functions:

```python
data_mapping = {
    "status": _get_status_data,
    "vacuum": _get_vacuum_data,
    "led": _get_led_data,
    "rssi": _get_rssi_data,
    "filter_status": _get_filter_status_data,
    # ... and more
}
```

When an entity requests data, the appropriate handler:

1. Accesses AWS IoT data and/or REST API data
2. Processes and transforms the raw data
3. Returns a dictionary with state, attributes, and available actions

### User Actions

User actions are implemented as async methods in the coordinator and include:

#### Vacuum Actions

- **Start**: `_vacuum_start()` - Starts cleaning with current mode
- **Pause**: `_vacuum_pause()` - Pauses the cleaning cycle
- **Return to Base**: `_pickup()` - Returns robot to dock
- **Set Fan Speed**: `_set_cleaning_mode()` - Changes cleaning mode
- **Locate**: `_vacuum_locate()` - Enables LED for locating

#### LED Actions

- **Turn On/Off**: `_set_led_enabled()` / `_set_led_disabled()`
- **Set Intensity**: `_set_led_intensity()`
- **Set Mode**: `_set_led_mode()` - Change LED pattern

#### Remote Control Actions

- **Send Command**: `_set_joystick_mode()` - Manual directional control
- **Exit Manual Mode**: `_exit_joystick_mode()`

### Command Types

#### Desired State Commands

Published to `shadow/update` to change persistent device state:

- Cleaning mode
- LED settings
- Schedule configuration
- Cycle time

#### Dynamic Commands

Published to `shadow/update/documents` for immediate, non-persistent actions:

- Joystick control (forward, backward, left, right)
- Pause/resume
- Pickup

---

## Summary

The MyDolphin Plus integration uses a **robust, multi-layered approach**:

### Core Flows

1. **Startup Flow**: Initial authentication and connection establishment
2. **Recovery Flow**: Automatic reconnection with exponential backoff (1-15 min)
3. **Update Flows**:
   - Periodic REST API calls for static information (5 min interval, once per connection)
   - Periodic MQTT shadow requests for reliable state sync (30 sec interval)
   - Real-time MQTT push notifications with debouncing for immediate updates (async, 1s debounce, 5s max delay)
   - On-demand entity requests for user actions and state queries

### Architecture Benefits

- **Reliability**: Periodic polling ensures state is eventually consistent; automatic recovery handles failures
- **Responsiveness**: Real-time MQTT provides immediate updates with debouncing (1s cooldown, 5s max delay); sub-second command execution
- **Efficiency**: Smart caching (device details once per connection), rate limiting, and backoff strategies minimize unnecessary API calls
- **Resilience**: Exponential backoff and automatic recovery ensure graceful handling of network/service disruptions
- **Flexibility**: Separate update mechanisms can be tuned independently; recovery is transparent to users

---

## Configuration

Key timing constants (from `consts.py`):

```python
# Update Intervals
UPDATE_ENTITIES_INTERVAL = timedelta(seconds=5)   # Entity refresh rate
UPDATE_API_INTERVAL = timedelta(minutes=5)        # REST API check interval
UPDATE_WS_INTERVAL = timedelta(seconds=30)        # MQTT shadow request interval

# Recovery Settings
RECONNECT_BACKOFF_MAX = timedelta(minutes=15)     # Maximum backoff time
MIN_TOKEN_FETCH_INTERVAL = timedelta(minutes=5)   # Rate limit for token refresh
AWS_CREDENTIALS_TTL = timedelta(hours=1)          # AWS IoT credential lifetime
```

**Update Intervals**: Control how frequently the coordinator checks for updates and refreshes data.

**Recovery Settings**:

- `RECONNECT_BACKOFF_MAX`: Caps exponential backoff at 15 minutes
- `MIN_TOKEN_FETCH_INTERVAL`: Prevents excessive API calls during token refresh attempts
- `AWS_CREDENTIALS_TTL`: AWS IoT credentials expire after 1 hour, triggering automatic refresh

These can be adjusted based on your needs for responsiveness vs. resource usage.
