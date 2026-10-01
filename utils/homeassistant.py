#!/usr/bin/env python3
import re
import requests
import config
import os
from utils import audio
from utils.timing import time_execution

@time_execution(label="API request to HomeAssistant")
def send_homeassistant_command(entity_id, service):
    """
    Send a command to Home Assistant to control an entity.
    
    Args:
        entity_id (str): The entity ID to control
        service (str): The service to call (turn_on, turn_off, etc.)
        
    Returns:
        bool: True if the command was successful, False otherwise
    """
    headers = {
        "Authorization": f"Bearer {config.HOMEASSISTANT_TOKEN}",
        "Content-Type": "application/json",
    }
    
    # entity_id may be a single string or a list of IDs sharing a domain
    first = entity_id[0] if isinstance(entity_id, (list, tuple)) else entity_id
    domain = first.split('.')[0]

    url = f"{config.HOMEASSISTANT_URL}/api/services/{domain}/{service}"

    data = {
        "entity_id": list(entity_id) if isinstance(entity_id, (list, tuple)) else entity_id
    }
    
    try:
        # rarely homeassistan succeds to send the command, but connectiong hangs
        # to avoid user frustration, we terminate the connection after 5 seconds
        response = requests.post(url, headers=headers, json=data, timeout=5)
        response.raise_for_status()
        print(f"Successfully sent command to Home Assistant: {service} {entity_id}")
        return True
    except Exception as e:
        print(f"Error sending command to Home Assistant: {e}")
        return False

def alias_in(alias, transcript):
    """An alias is a substring, or a compiled regex that must be found in the
    transcript. Regexes let config express what a substring can't, e.g. an
    English particle verb split around its object ("turn the light on")."""
    if isinstance(alias, re.Pattern):
        return alias.search(transcript) is not None
    return alias in transcript


@time_execution(label="Check if it's HomeAssistant command")
def process_command(transcript, source_name=None):
    """Match a transcript to a command, logging each matching step. See match_command.

    Returns (success, entity_id, action).
    """
    return match_command(transcript, source_name)[:3]


def match_command(transcript, source_name=None, log=print):
    """
    Process the transcribed text to check if it matches action, device, and room aliases.
    If matches are found, identify the entity_id and action for the command.

    Args:
        transcript (str): The transcribed text to process
        source_name (str, optional): Name of the audio source the transcript came
            from. If `config.source_rooms` maps it to a room, that room is used
            as the default when the utterance doesn't name a room.
        log (callable, optional): Receives each matching step as a message.
            Window mode passes a no-op: it matches several transcripts a second.

    Returns:
        tuple: (success, entity_id, action, room_from_mic)
            - success (bool): True if a command was matched, False otherwise
            - entity_id (str): The entity ID to control
            - action (str): The action to perform
            - room_from_mic (bool): True if no room was named and the target
              came from the mic's default room. Window mode uses it to prefer,
              among readings of one phrase, those that name the room: a room
              word that one mic heard garbled ("in the gordon") must not send
              the command to that mic's own room when another mic heard "in
              the garden".
    """
    if not transcript:
        return False, None, None, False

    # Check if we have the necessary configuration
    required_attrs = ['action_aliases', 'device_aliases', 'room_entities', 'default_room']
    if not all(hasattr(config, attr) for attr in required_attrs):
        log("Missing required configuration attributes")
        return False, None, None, False
    
    # Convert transcript to lowercase for case-insensitive matching
    transcript = transcript.lower().strip()
    
    # Find action in transcript
    action = None
    for action_name, aliases in config.action_aliases.items():
        if any(alias_in(alias, transcript) for alias in aliases):
            action = action_name
            log(f"Action recognized: {action}")
            break
    
    if not action:
        log("No action recognized in transcript")
        return False, None, None, False
    
    # Find device in transcript
    device = None
    for device_name, aliases in config.device_aliases.items():
        if any(alias_in(alias, transcript) for alias in aliases):
            device = device_name
            log(f"Device recognized: {device}")
            break
    
    if not device:
        log("No device recognized in transcript")
        return False, None, None, False
    
    # Find room in transcript (optional)
    room_specified = False
    source_rooms = getattr(config, 'source_rooms', {})
    room = source_rooms.get(source_name, config.default_room)
    for room_name, aliases in config.room_aliases.items():
        if any(alias_in(alias, transcript) for alias in aliases):
            room = room_name
            room_specified = True
            log(f"Room recognized: {room}")
            break
    
    # Check if this device can be used without specifying a room
    room_from_mic = False
    if not room_specified and hasattr(config, 'devices_without_room') and device in config.devices_without_room:
        # Search for the device across all rooms
        entity_id = None
        for search_room, devices in config.room_entities.items():
            if device in devices:
                entity_id = devices[device]
                room = search_room
                log(f"Found {device} in {room} without room specification")
                break
        
        if entity_id is None:
            log(f"No entity found for {device} in any room")
            return False, None, None, False
    else:
        room_from_mic = not room_specified
        # Get entity ID for the device in the specified room
        if room not in config.room_entities or device not in config.room_entities[room]:
            log(f"No entity found for {device} in {room}")
            return False, None, None, False
        
        entity_id = config.room_entities[room][device]

    # An entity mapping may be action-specific: a dict of {action: entity(s)}
    # lets one action target a different set than another — e.g. turning the
    # pool light *off* also kills the jacuzzi, while turning it *on* does not.
    # A "default" key covers any action not listed explicitly.
    if isinstance(entity_id, dict):
        if action in entity_id:
            entity_id = entity_id[action]
        elif 'default' in entity_id:
            entity_id = entity_id['default']
        else:
            log(f"No entities mapped for action '{action}' on {device} in {room}")
            return False, None, None, False

    log(f"Executing action: {action} on {entity_id} in {room}")
    
    # No longer sending the command here, as it will be sent from main.py
    # Return the values for main.py to use
    return True, entity_id, action, room_from_mic
