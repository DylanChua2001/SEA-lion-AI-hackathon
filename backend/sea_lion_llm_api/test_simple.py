#!/usr/bin/env python3
"""
Simple test script for SEA-LION chatbot
"""
import requests
import json
import uuid

def test_health():
    """Test health endpoint"""
    print("=== Testing Health ===")
    try:
        response = requests.get("http://54.151.209.144:8001/healthz", timeout=5)
        print(f"Status: {response.status_code}")
        print(f"Response: {response.json()}")
        return True
    except Exception as e:
        print(f"Health check failed: {e}")
        return False

def test_chat():
    """Test chat endpoint"""
    print("\n=== Testing Chat ===")
    
    # Generate a random session ID (MongoDB ObjectId format)
    session_id = str(uuid.uuid4()).replace("-", "")[:24]
    
    payload = {
        "sessionId": session_id,
        "text": "Hello! Can you help me book an appointment?"
    }
    
    print(f"Session ID: {session_id}")
    print(f"Payload: {json.dumps(payload, indent=2)}")
    
    try:
        response = requests.post(
            "http://54.151.209.144:8001/chat", 
            json=payload, 
            timeout=30
        )
        print(f"Status: {response.status_code}")
        
        if response.status_code == 200:
            data = response.json()
            print(f"Reply: {data.get('reply', 'No reply found')}")
            return True
        else:
            print(f"Error: {response.text}")
            return False
            
    except Exception as e:
        print(f"Chat test failed: {e}")
        return False

def test_openai_compatible():
    """Test OpenAI-compatible endpoint"""
    print("\n=== Testing OpenAI-Compatible Endpoint ===")
    
    payload = {
        "model": "aisingapore/Gemma-SEA-LION-v3-9B-IT",
        "messages": [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Hello! How are you?"}
        ],
        "temperature": 0.7
    }
    
    print(f"Payload: {json.dumps(payload, indent=2)}")
    
    try:
        response = requests.post(
            "http://54.151.209.144:8001/v1/chat/completions", 
            json=payload, 
            timeout=30
        )
        print(f"Status: {response.status_code}")
        
        if response.status_code == 200:
            data = response.json()
            print(f"Response: {json.dumps(data, indent=2)}")
            return True
        else:
            print(f"Error: {response.text}")
            return False
            
    except Exception as e:
        print(f"OpenAI-compatible test failed: {e}")
        return False

if __name__ == "__main__":
    print("=== SEA-LION Chatbot Test ===")
    
    # Test all endpoints
    health_ok = test_health()
    chat_ok = test_chat()
    openai_ok = test_openai_compatible()
    
    print("\n=== Test Results ===")
    print(f"Health: {'✓' if health_ok else '✗'}")
    print(f"Chat: {'✓' if chat_ok else '✗'}")
    print(f"OpenAI-compatible: {'✓' if openai_ok else '✗'}")
    
    if all([health_ok, chat_ok, openai_ok]):
        print("\n🎉 All tests passed! Your SEA-LION chatbot is working.")
    else:
        print("\n❌ Some tests failed. Check the logs above.")
