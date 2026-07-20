FUNCTION onLoad
    LOGIC
        setStore1: UIEngine.SetStore(path = "Page.unitsData", value = [])
            output
                setStore: UIEngine.SetStore(path = "Page.towersData", value = [{
    "description": "Good Tower",
    "name": "Tower-001",
    "floors": 36,
    "open": false,
    "maxLength": 1,
    "floorData": [
        {
            "floorNumber": {},
            "floorName": "Unknown"
        },
        {
            "floorNumber": {},
            "floorName": "Unknown"
        },
        {
            "floorNumber": {},
            "floorName": "Unknown"
        },
        {
            "floorNumber": {
                "floor": [
                    "A3200"
                ],
                "status": [
                    "Booked"
                ],
                "unitType": [
                    "Individual unit"
                ]
            },
            "floorName": "Unknown"
        },
        {
            "floorNumber": {},
            "floorName": "Unknown"
        },
        {
            "floorNumber": {},
            "floorName": "Thirtieth"
        },
        {
            "floorNumber": {},
            "floorName": "Twenty-ninth"
        },
        {
            "floorNumber": {},
            "floorName": "Twenty-eighth"
        },
        {
            "floorNumber": {},
            "floorName": "Twenty-seventh"
        },
        {
            "floorNumber": {},
            "floorName": "Twenty-sixth"
        },
        {
            "floorNumber": {},
            "floorName": "Twenty-fifth"
        },
        {
            "floorNumber": {},
            "floorName": "Twenty-fourth"
        },
        {
            "floorNumber": {},
            "floorName": "Twenty-third"
        },
        {
            "floorNumber": {
                "floor": [
                    "A2200"
                ],
                "status": [
                    "Available"
                ],
                "unitType": [
                    "Individual unit"
                ]
            },
            "floorName": "Twenty-second"
        },
        {
            "floorNumber": {
                "floor": [
                    "A2100"
                ],
                "status": [
                    "Available"
                ],
                "unitType": [
                    "Individual unit"
                ]
            },
            "floorName": "Twenty-first"
        },
        {
            "floorNumber": {},
            "floorName": "Twentieth"
        },
        {
            "floorNumber": {},
            "floorName": "Nineteenth"
        },
        {
            "floorNumber": {},
            "floorName": "Eighteenth"
        },
        {
            "floorNumber": {},
            "floorName": "Seventeenth"
        },
        {
            "floorNumber": {},
            "floorName": "Sixteenth"
        },
        {
            "floorNumber": {},
            "floorName": "Fifteenth"
        },
        {
            "floorNumber": {},
            "floorName": "Fourteenth"
        },
        {
            "floorNumber": {},
            "floorName": "Thirteenth"
        },
        {
            "floorNumber": {},
            "floorName": "Twelfth"
        },
        {
            "floorNumber": {
                "floor": [
                    "A1100"
                ],
                "status": [
                    "Blocked"
                ],
                "unitType": [
                    "Duplex"
                ]
            },
            "floorName": "Eleventh"
        },
        {
            "floorNumber": {},
            "floorName": "Tenth"
        },
        {
            "floorNumber": {},
            "floorName": "Ninth"
        },
        {
            "floorNumber": {},
            "floorName": "Eighth"
        },
        {
            "floorNumber": {},
            "floorName": "Seventh"
        },
        {
            "floorNumber": {},
            "floorName": "Sixth"
        },
        {
            "floorNumber": {},
            "floorName": "Fifth"
        },
        {
            "floorNumber": {},
            "floorName": "Fourth"
        },
        {
            "floorNumber": {},
            "floorName": "Third"
        },
        {
            "floorNumber": {
                "floor": [
                    "A200"
                ],
                "status": [
                    "Available"
                ],
                "unitType": [
                    "Duplex"
                ]
            },
            "floorName": "Second"
        },
        {
            "floorNumber": {},
            "floorName": "First"
        },
        {
            "floorNumber": {},
            "floorName": "Ground"
        }
    ],
    "unit": [
        {
            "unitType": "Individual unit",
            "floorNumber": 32,
            "unitNumber": "A3200",
            "bookingStatus": "Booked",
            "unitConfiguration": "1BHK + 3T",
            "superBuiltupArea": 8679,
            "commonProportionateArea": 89,
            "reraCarpetArea": 765,
            "facing": "West",
            "UDS": 687,
            "carParking": 2,
            "plcEast": "Available",
            "plcClubHouseFacing": "Unavailable"
        },
        {
            "unitType": "Individual unit",
            "floorNumber": 22,
            "unitNumber": "A2200",
            "bookingStatus": "Available",
            "unitConfiguration": "3BHK + 3T",
            "superBuiltupArea": 87,
            "commonProportionateArea": 5647,
            "reraCarpetArea": 8978,
            "facing": "West",
            "UDS": 456,
            "carParking": 2,
            "plcEast": "Available",
            "plcClubHouseFacing": "Available"
        },
        {
            "unitType": "Duplex",
            "floorNumber": 11,
            "unitNumber": "A1100",
            "bookingStatus": "Blocked",
            "unitConfiguration": "2BHK + 3T",
            "superBuiltupArea": 876,
            "commonProportionateArea": 987,
            "reraCarpetArea": 453,
            "facing": "West",
            "UDS": 687,
            "carParking": 2,
            "plcEast": "Available",
            "plcClubHouseFacing": "Available"
        },
        {
            "unitType": "Individual unit",
            "floorNumber": 21,
            "unitNumber": "A2100",
            "bookingStatus": "Available",
            "unitConfiguration": "5BHK + 3T",
            "reraCarpetArea": 867,
            "facing": "West",
            "UDS": 687,
            "commonProportionateArea": 68,
            "carParking": 2,
            "plcEast": "Unavailable",
            "plcClubHouseFacing": "Unavailable"
        },
        {
            "unitType": "Duplex",
            "floorNumber": 2,
            "unitNumber": "A200",
            "bookingStatus": "Available",
            "unitConfiguration": "4BHK + 1T",
            "reraCarpetArea": 678,
            "facing": "West",
            "UDS": 87,
            "carParking": 2,
            "plcEast": "Available",
            "plcClubHouseFacing": "Unavailable"
        }
    ],
    "unitsData": [
        [
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "Individual unit",
                "unitConfiguration": "1BHK + 3T",
                "unitNumber": "A3200",
                "bookingStatus": "Booked"
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "Individual unit",
                "unitConfiguration": "3BHK + 3T",
                "unitNumber": "A2200",
                "bookingStatus": "Available"
            },
            {
                "unitType": "Individual unit",
                "unitConfiguration": "5BHK + 3T",
                "unitNumber": "A2100",
                "bookingStatus": "Available"
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "Duplex",
                "unitConfiguration": "2BHK + 3T",
                "unitNumber": "A1100",
                "bookingStatus": "Blocked"
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "Duplex",
                "unitConfiguration": "4BHK + 1T",
                "unitNumber": "A200",
                "bookingStatus": "Available"
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            }
        ]
    ]
}, {
    "description": "Good tower",
    "name": "Tower-002",
    "floors": 33,
    "open": false,
    "maxLength": 2,
    "floorData": [
        {
            "floorNumber": {},
            "floorName": "Unknown"
        },
        {
            "floorNumber": {},
            "floorName": "Unknown"
        },
        {
            "floorNumber": {},
            "floorName": "Thirtieth"
        },
        {
            "floorNumber": {},
            "floorName": "Twenty-ninth"
        },
        {
            "floorNumber": {},
            "floorName": "Twenty-eighth"
        },
        {
            "floorNumber": {},
            "floorName": "Twenty-seventh"
        },
        {
            "floorNumber": {},
            "floorName": "Twenty-sixth"
        },
        {
            "floorNumber": {},
            "floorName": "Twenty-fifth"
        },
        {
            "floorNumber": {},
            "floorName": "Twenty-fourth"
        },
        {
            "floorNumber": {},
            "floorName": "Twenty-third"
        },
        {
            "floorNumber": {},
            "floorName": "Twenty-second"
        },
        {
            "floorNumber": {},
            "floorName": "Twenty-first"
        },
        {
            "floorNumber": {},
            "floorName": "Twentieth"
        },
        {
            "floorNumber": {},
            "floorName": "Nineteenth"
        },
        {
            "floorNumber": {},
            "floorName": "Eighteenth"
        },
        {
            "floorNumber": {},
            "floorName": "Seventeenth"
        },
        {
            "floorNumber": {},
            "floorName": "Sixteenth"
        },
        {
            "floorNumber": {},
            "floorName": "Fifteenth"
        },
        {
            "floorNumber": {},
            "floorName": "Fourteenth"
        },
        {
            "floorNumber": {},
            "floorName": "Thirteenth"
        },
        {
            "floorNumber": {},
            "floorName": "Twelfth"
        },
        {
            "floorNumber": {},
            "floorName": "Eleventh"
        },
        {
            "floorNumber": {},
            "floorName": "Tenth"
        },
        {
            "floorNumber": {},
            "floorName": "Ninth"
        },
        {
            "floorNumber": {
                "floor": [
                    "A800"
                ],
                "status": [
                    "Booked"
                ],
                "unitType": [
                    "Duplex"
                ]
            },
            "floorName": "Eighth"
        },
        {
            "floorNumber": {},
            "floorName": "Seventh"
        },
        {
            "floorNumber": {},
            "floorName": "Sixth"
        },
        {
            "floorNumber": {
                "floor": [
                    "A500"
                ],
                "status": [
                    "Blocked"
                ],
                "unitType": [
                    "Duplex"
                ]
            },
            "floorName": "Fifth"
        },
        {
            "floorNumber": {},
            "floorName": "Fourth"
        },
        {
            "floorNumber": {
                "floor": [
                    "A300",
                    "A301"
                ],
                "status": [
                    "Available",
                    "Available"
                ],
                "unitType": [
                    "Individual unit",
                    "Individual unit"
                ]
            },
            "floorName": "Third"
        },
        {
            "floorNumber": {},
            "floorName": "Second"
        },
        {
            "floorNumber": {},
            "floorName": "First"
        },
        {
            "floorNumber": {},
            "floorName": "Ground"
        }
    ],
    "unit": [
        {
            "unitType": "Individual unit",
            "floorNumber": 3,
            "unitNumber": "A300",
            "bookingStatus": "Available",
            "unitConfiguration": "2BHK + 3T",
            "superBuiltupArea": 876,
            "commonProportionateArea": 456,
            "reraCarpetArea": 978,
            "facing": "West",
            "UDS": 866,
            "carParking": 2,
            "plcEast": "Available",
            "plcClubHouseFacing": "Unavailable"
        },
        {
            "unitType": "Duplex",
            "floorNumber": 5,
            "unitNumber": "A500",
            "bookingStatus": "Blocked",
            "unitConfiguration": "2BHK + 3T",
            "superBuiltupArea": 687,
            "commonProportionateArea": 46,
            "reraCarpetArea": 978,
            "facing": "West",
            "UDS": 768,
            "carParking": 2,
            "plcEast": "Unavailable",
            "plcClubHouseFacing": "Available"
        },
        {
            "unitType": "Individual unit",
            "floorNumber": 3,
            "unitNumber": "A301",
            "bookingStatus": "Available",
            "unitConfiguration": "2BHK + 1T",
            "superBuiltupArea": 867,
            "commonProportionateArea": 576,
            "reraCarpetArea": 3435,
            "facing": "West",
            "UDS": 867,
            "carParking": 2,
            "plcEast": "Available",
            "plcClubHouseFacing": "Unavailable"
        },
        {
            "unitType": "Duplex",
            "floorNumber": 8,
            "unitNumber": "A800",
            "bookingStatus": "Booked",
            "unitConfiguration": "2BHK + 1T",
            "superBuiltupArea": 456,
            "commonProportionateArea": 897,
            "reraCarpetArea": 355,
            "facing": "West",
            "UDS": 856,
            "carParking": 2,
            "plcEast": "Available",
            "plcClubHouseFacing": "Unavailable"
        }
    ],
    "unitsData": [
        [
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "Duplex",
                "unitConfiguration": "2BHK + 1T",
                "unitNumber": "A800",
                "bookingStatus": "Booked"
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "Duplex",
                "unitConfiguration": "2BHK + 3T",
                "unitNumber": "A500",
                "bookingStatus": "Blocked"
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "Individual unit",
                "unitConfiguration": "2BHK + 3T",
                "unitNumber": "A300",
                "bookingStatus": "Available"
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            }
        ],
        [
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "Individual unit",
                "unitConfiguration": "2BHK + 1T",
                "unitNumber": "A301",
                "bookingStatus": "Available"
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            }
        ]
    ]
}, {
    "description": "good towers with great amenities",
    "name": "Tower-003",
    "floors": 24,
    "open": false,
    "maxLength": 1,
    "floorData": [
        {
            "floorNumber": {},
            "floorName": "Twenty-third"
        },
        {
            "floorNumber": {},
            "floorName": "Twenty-second"
        },
        {
            "floorNumber": {},
            "floorName": "Twenty-first"
        },
        {
            "floorNumber": {},
            "floorName": "Twentieth"
        },
        {
            "floorNumber": {},
            "floorName": "Nineteenth"
        },
        {
            "floorNumber": {},
            "floorName": "Eighteenth"
        },
        {
            "floorNumber": {},
            "floorName": "Seventeenth"
        },
        {
            "floorNumber": {},
            "floorName": "Sixteenth"
        },
        {
            "floorNumber": {},
            "floorName": "Fifteenth"
        },
        {
            "floorNumber": {},
            "floorName": "Fourteenth"
        },
        {
            "floorNumber": {},
            "floorName": "Thirteenth"
        },
        {
            "floorNumber": {},
            "floorName": "Twelfth"
        },
        {
            "floorNumber": {},
            "floorName": "Eleventh"
        },
        {
            "floorNumber": {},
            "floorName": "Tenth"
        },
        {
            "floorNumber": {},
            "floorName": "Ninth"
        },
        {
            "floorNumber": {},
            "floorName": "Eighth"
        },
        {
            "floorNumber": {
                "floor": [
                    "A700"
                ],
                "status": [
                    "Available"
                ],
                "unitType": [
                    "Individual unit"
                ]
            },
            "floorName": "Seventh"
        },
        {
            "floorNumber": {},
            "floorName": "Sixth"
        },
        {
            "floorNumber": {},
            "floorName": "Fifth"
        },
        {
            "floorNumber": {},
            "floorName": "Fourth"
        },
        {
            "floorNumber": {},
            "floorName": "Third"
        },
        {
            "floorNumber": {},
            "floorName": "Second"
        },
        {
            "floorNumber": {},
            "floorName": "First"
        },
        {
            "floorNumber": {},
            "floorName": "Ground"
        }
    ],
    "unit": [
        {
            "unitType": "Individual unit",
            "floorNumber": 7,
            "unitNumber": "A700",
            "bookingStatus": "Available",
            "unitConfiguration": "2BHK + 1T",
            "superBuiltupArea": 576,
            "commonProportionateArea": 543,
            "reraCarpetArea": 978,
            "facing": "North",
            "UDS": 234,
            "carParking": 2,
            "plcEast": "Unavailable",
            "plcClubHouseFacing": "Unavailable"
        }
    ],
    "unitsData": [
        [
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "Individual unit",
                "unitConfiguration": "2BHK + 1T",
                "unitNumber": "A700",
                "bookingStatus": "Available"
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            },
            {
                "unitType": "",
                "unitConfiguration": "",
                "unitNumber": "",
                "bookingStatus": ""
            }
        ]
    ]
}]) AFTER Steps.setStore1.output
                    output
                        forEachLoop: System.Loop.ForEachLoop(source = Page.towersData) AFTER Steps.setStore.output
                            iteration
                                forEachLoop1: System.Loop.ForEachLoop(source = Steps.forEachLoop.iteration.each.unitsData)
                                    iteration
                                        setStore2: UIEngine.SetStore(path = `'Page.unitsData[{{Steps.forEachLoop1.iteration.index}}].data'`, value = Steps.forEachLoop1.iteration.each)
                                    output
                                        setStore3: UIEngine.SetStore(path = `'Page.towersData[{{Steps.forEachLoop.iteration.index}}].unitsData'`, value = Page.unitsData) AFTER Steps.forEachLoop1.output
                                            output
                                                setStore4: UIEngine.SetStore(path = "Page.unitsData", value = []) AFTER Steps.setStore3.output
                            output
                                checking_max_floorNumber: _.checking_max_floorNumber() AFTER Steps.forEachLoop.output