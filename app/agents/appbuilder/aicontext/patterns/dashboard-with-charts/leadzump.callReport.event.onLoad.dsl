FUNCTION onLoad
    LOGIC
        setStore: UIEngine.SetStore(path = "Page.filterObject", value = {
    "reportedType": [
        {
            "toggle": false,
            "lable": "By Business Partner"
        },
        {
            "toggle": false,
            "lable": "By Product"
        }
    ],
    "callDirection": [
        {
            "toggle": false,
            "lable": "Inbound"
        },
        {
            "toggle": false,
            "lable": "Outbound"
        }
    ],
    "Product": [
        {
            "toggle": false,
            "lable": "Cityville"
        }
    ],
    "assignedUsers": [
        {
            "toggle": false,
            "lable": "Siddharth"
        },
        {
            "toggle": false,
            "lable": "Rahul"
        }
    ]
})
        setStore1: UIEngine.SetStore(path = "Page.callDetailsReport", value = [{
    "name": "Neetu Saxsena",
    "totalCall": 80,
    "outgoingCalls": {
        "total": 65,
        "connected": 56
    },
    "incomingCalls": {
        "total": 15,
        "connected": 10,
        "missed": 5
    },
    "totalConnectedCalls": 66,
    "totalCallDuration": "01:48"
}, {
    "name": "Muskan Sai",
    "totalCall": 100,
    "outgoingCalls": {
        "total": 87,
        "connected": 73
    },
    "incomingCalls": {
        "total": 13,
        "connected": 8,
        "missed": 5
    },
    "totalConnectedCalls": 81,
    "totalCallDuration": "02:20"
}, {
    "name": "Steve Jobs",
    "totalCall": 120,
    "outgoingCalls": {
        "total": 93,
        "connected": 87
    },
    "incomingCalls": {
        "total": 27,
        "connected": 14,
        "missed": 13
    },
    "totalConnectedCalls": 101,
    "totalCallDuration": "01:48"
}, {
    "name": "Neha Nagar",
    "totalCall": 43,
    "outgoingCalls": {
        "total": 32,
        "connected": 30
    },
    "incomingCalls": {
        "total": 11,
        "connected": 4,
        "missed": 7
    },
    "totalConnectedCalls": 34,
    "totalCallDuration": "01:48"
}, {
    "name": "Saroj Kumar Rout",
    "totalCall": 123,
    "outgoingCalls": {
        "total": 100,
        "connected": 90
    },
    "incomingCalls": {
        "total": 23,
        "connected": 9,
        "missed": 14
    },
    "totalConnectedCalls": 99,
    "totalCallDuration": "01:48"
}, {
    "name": "Mohana Patil",
    "totalCall": 150,
    "outgoingCalls": {
        "total": 120,
        "connected": 111
    },
    "incomingCalls": {
        "total": 30,
        "connected": 21,
        "missed": 9
    },
    "totalConnectedCalls": 131,
    "totalCallDuration": "01:48"
}, {
    "name": "John Snow",
    "totalCall": 53,
    "outgoingCalls": {
        "total": 42,
        "connected": 35
    },
    "incomingCalls": {
        "total": 11,
        "connected": 8,
        "missed": 2
    },
    "totalConnectedCalls": 43,
    "totalCallDuration": "01:48"
}, {
    "name": "Rumman Yezdani",
    "totalCall": 90,
    "outgoingCalls": {
        "total": 80,
        "connected": 75
    },
    "incomingCalls": {
        "total": 10,
        "connected": 6,
        "missed": 4
    },
    "totalConnectedCalls": 81,
    "totalCallDuration": "01:48"
}, {
    "name": "Joanna Harris",
    "totalCall": 100,
    "outgoingCalls": {
        "total": 98,
        "connected": 80
    },
    "incomingCalls": {
        "total": 2,
        "connected": 2,
        "missed": 0
    },
    "totalConnectedCalls": 82,
    "totalCallDuration": "01:10"
}, {
    "name": "Louis Andrews",
    "totalCall": 124,
    "outgoingCalls": {
        "total": 110,
        "connected": 99
    },
    "incomingCalls": {
        "total": 14,
        "connected": 14,
        "missed": 0
    },
    "totalConnectedCalls": 113,
    "totalCallDuration": "02:35"
}])