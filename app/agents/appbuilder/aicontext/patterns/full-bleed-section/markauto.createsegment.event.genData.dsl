FUNCTION genData
    LOGIC
        loginStep: UIEngine.SetStore(path = "Store.buttonbarData", value = [{
    "name": "Raja",
    "id": 1,
    "department": "IT",
    "links": {
        "a": {
            "b": "Raja_Inner"
        }
    },
    "originalObjectKey": 0
}, {
    "name": "Avinash",
    "id": 2,
    "department": "CS",
    "links": {
        "a": {
            "b": "Avinash_Inner"
        }
    },
    "originalObjectKey": 1
}, {
    "name": "Alli",
    "id": 3,
    "department": "EE",
    "links": {
        "a": {
            "b": "Alli_Inner"
        }
    },
    "originalObjectKey": 2
}, {
    "name": "Akhilesh",
    "id": 4,
    "department": "EC",
    "links": {
        "a": {
            "b": "Akhilesh_Inner"
        }
    },
    "originalObjectKey": 3
}, {
    "name": "Surendhar",
    "id": 5,
    "department": "IT",
    "links": {
        "a": {
            "b": "Surendhar_Inner"
        }
    },
    "originalObjectKey": 4
}, {
    "name": "Kiran",
    "id": 6,
    "department": "IT",
    "links": {
        "a": {
            "b": "Kiran_Inner"
        }
    },
    "originalObjectKey": 5
}])
            output
                genOutput: System.GenerateEvent() AFTER Steps.loginStep.output