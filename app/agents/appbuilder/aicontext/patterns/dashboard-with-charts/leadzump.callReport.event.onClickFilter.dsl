FUNCTION onClickFilter
    LOGIC
        filtering: UIEngine.SetStore(path = "Page.filter", value = not Page.filter)
            output
                collapse: UIEngine.SetStore(path = "Page.collapse", value = true) AFTER Steps.filtering.output
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