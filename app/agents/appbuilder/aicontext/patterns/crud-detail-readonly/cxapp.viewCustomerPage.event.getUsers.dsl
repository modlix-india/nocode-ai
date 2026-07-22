FUNCTION getUsers
    LOGIC
        setStore2: UIEngine.SetStore(path = "Page.loader", value = true)
            output
                tableEmptyGrid: UIEngine.SetStore(path = "Page.emptyGrid", value = false) AFTER Steps.setStore2.output
                    output
                        setStore14: UIEngine.SetStore(path = "Page.showNoCustomer", value = "customer") AFTER Steps.tableEmptyGrid.output
                            output
                                if: System.If(condition = Url.pathParts[1]) AFTER Steps.setStore14.output
                                    true
                                        getUsers_copy_1: _.getUsers_copy_1() AFTER Steps.if.true
                                    false
                                        setStore3: UIEngine.SetStore(path = "Page.userFilter", value = {
    "condition": {
        "field": "clientId",
        "negate": true
    }
}) AFTER Steps.if.false
                                            output
                                                setStore4: UIEngine.SetStore(path = "Page.userFilter.condition.value", value = Store.auth.loggedInClientId) AFTER Steps.setStore3.output
                                                    output
                                                        setStore5: UIEngine.SetStore(path = "Page.userFilter.page", value = Page.userData.number ?? 0) AFTER Steps.setStore4.output
                                                            output
                                                                setStore6: UIEngine.SetStore(path = "Page.userFilter.size", value = Page.userData.size ??  5) AFTER Steps.setStore5.output
                                                                    output
                                                                        fetchData: UIEngine.SendData(url = "api/security/users/query", method = "POST", payload = Page.userFilter) AFTER Steps.setStore6.output
                                                                            output
                                                                                setStore11: UIEngine.SetStore(path = "Page.custIds", value = []) AFTER Steps.fetchData.output
                                                                                setStore: UIEngine.SetStore(path = `'Page.userData'`, value = Steps.fetchData.output.data)
                                                                                    output
                                                                                        if1: System.If(condition = Page.userData.content != undefined) AFTER Steps.setStore.output
                                                                                            true
                                                                                                forEachLoop: System.Loop.ForEachLoop(source = Page.userData.content) AFTER Steps.if1.true
                                                                                                    iteration
                                                                                                        setStore10: UIEngine.SetStore(path = `'Page.custIds[{{Steps.forEachLoop.iteration.index}}]'`, value = Steps.forEachLoop.iteration.each.id)
                                                                                                    output
                                                                                                        customer: cxapp.customer(userIds = Page.custIds) AFTER Steps.forEachLoop.output
                                                                                                            error
                                                                                                                loaderDisabling: UIEngine.SetStore(path = "Page.loader", value = false) AFTER Steps.customer.error
                                                                                                                    output
                                                                                                                        tableEmptyGrid_Copy_1: UIEngine.SetStore(path = "Page.emptyGrid", value = true) AFTER Steps.loaderDisabling.output
                                                                                                            output
                                                                                                                setStore1: UIEngine.SetStore(path = "Page.customersData", value = Steps.customer.output.result)
                                                                                                        setStore2_Copy_1: UIEngine.SetStore(path = "Page.loader", value = false) AFTER Steps.forEachLoop.output
                                                                                            false
                                                                                                tableEmptyGrid_Copy_2: UIEngine.SetStore(path = "Page.emptyGrid", value = true) AFTER Steps.if1.false
                                                                                                setStore2_Copy_2: UIEngine.SetStore(path = "Page.loader", value = false) AFTER Steps.if1.false
                                                                                        if2: System.If(condition =  not Page.userData.content.length < Page.userData.size) AFTER Steps.setStore.output
                                                                                            true
                                                                                                setStore12: UIEngine.SetStore(path = "Page.showPagination", value = "showPagination") AFTER Steps.if2.true
        setStore7: UIEngine.SetStore(path = "Page.Columns", value = false)
        setStore_Copy_1: UIEngine.SetStore(path = "Page.searchString", value = "")
        fetchData1: UIEngine.FetchData(url = `Url.pathParts[1]  ? 'api/ui/personalization/cxapp/{{Store.auth.client.code}}customersColumns{{Url.pathParts[1]}}' :   'api/ui/personalization/cxapp/{{Store.auth.client.code}}customersColumns'`)
            output
                objectEntries: System.Object.ObjectEntries(source = Steps.fetchData1.output.data)
                    output
                        if_Copy_1: System.If(condition = Steps.objectEntries.output.value.length !=0)
                            true
                                setStore9: UIEngine.SetStore(path = "Page.columns", value = Steps.fetchData1.output.data) AFTER Steps.if_Copy_1.true
                            false
                                setStore8: UIEngine.SetStore(path = "Page.columns", value = {
    "allColumns": false,
    "data": {
        "Project": {
            "color": "#43B2FF",
            "name": "Project",
            "checkbox": false,
            "icon": "api/files/static/file/SYSTEM/cxapp/viewCustomerIcons/1.svg"
        },
        "BookingDate": {
            "color": "#1CBA79",
            "name": "Booking Date",
            "checkbox": false,
            "icon": "api/files/static/file/SYSTEM/cxapp/viewCustomerIcons/2.svg"
        },
        "BookingMonth": {
            "color": "#DBA979",
            "name": "Booking Month",
            "checkbox": false,
            "icon": "api/files/static/file/SYSTEM/cxapp/viewCustomerIcons/3.svg"
        },
        "SourceofBookings": {
            "color": "#1CBA79",
            "name": "Source of Bookings",
            "checkbox": false,
            "icon": "api/files/static/file/SYSTEM/cxapp/viewCustomerIcons/4.svg"
        },
        "SubSource": {
            "color": "#93CAEE",
            "name": "Sub Source",
            "checkbox": false,
            "icon": "api/files/static/file/SYSTEM/cxapp/viewCustomerIcons/ss.svg"
        },
        "SBArea": {
            "color": "#FFC200",
            "name": "SB Area",
            "checkbox": false,
            "icon": "api/files/static/file/SYSTEM/cxapp/viewCustomerIcons/5.svg"
        },
        "CarpetArea": {
            "color": "#E672AB",
            "name": "Carpet Area",
            "checkbox": false,
            "icon": "api/files/static/file/SYSTEM/cxapp/viewCustomerIcons/6.svg"
        },
        "PropCommArea": {
            "color": "#FFBB70",
            "name": "Prop Comm Area",
            "checkbox": false,
            "icon": "api/files/static/file/SYSTEM/cxapp/viewCustomerIcons/7.svg"
        },
        "UDS": {
            "color": "#6AD4DD",
            "name": "UDS",
            "checkbox": false,
            "icon": "api/files/static/file/SYSTEM/cxapp/viewCustomerIcons/8.svg"
        },
        "SalesManager": {
            "color": "#D8B4F8",
            "name": "Sales Manager",
            "checkbox": false,
            "icon": "api/files/static/file/SYSTEM/cxapp/viewCustomerIcons/10.svg"
        },
        "PhoneNo": {
            "color": "#FF0000",
            "name": "Phone No",
            "checkbox": false,
            "icon": "api/files/static/file/SYSTEM/cxapp/viewCustomerIcons/11.svg"
        },
        "EmailID": {
            "color": "#FFBB70",
            "name": "Email ID",
            "checkbox": false,
            "icon": "api/files/static/file/SYSTEM/cxapp/viewCustomerIcons/12.svg"
        },
        "Address": {
            "color": "#6AD4DD",
            "name": "Address",
            "checkbox": false,
            "icon": "api/files/static/file/SYSTEM/cxapp/viewCustomerIcons/13.svg"
        },
        "Rate": {
            "color": "#03AED2",
            "name": "Rate",
            "checkbox": false,
            "icon": "api/files/static/file/SYSTEM/cxapp/viewCustomerIcons/14.svg"
        },
        "BaseRate": {
            "color": "#D8B4F8",
            "name": "Base Rate",
            "checkbox": false,
            "icon": "api/files/static/file/SYSTEM/cxapp/viewCustomerIcons/15.svg"
        },
        "TotalCost": {
            "color": "#03AED2",
            "name": "Total Cost",
            "checkbox": false,
            "icon": "api/files/static/file/SYSTEM/cxapp/viewCustomerIcons/16.svg"
        },
        "Taxable": {
            "color": "#D8B4F8",
            "name": "Taxable",
            "checkbox": false,
            "icon": "api/files/static/file/SYSTEM/cxapp/viewCustomerIcons/17.svg"
        },
        "GST": {
            "color": "#FF0000",
            "name": "GST",
            "checkbox": false,
            "icon": "api/files/static/file/SYSTEM/cxapp/viewCustomerIcons/18.svg"
        },
        "StampDuty": {
            "color": "#FFBB70",
            "name": "Stamp Duty",
            "checkbox": false,
            "icon": "api/files/static/file/SYSTEM/cxapp/viewCustomerIcons/19.svg"
        },
        "TotalCollection": {
            "color": "#6AD4DD",
            "name": "Total Collection",
            "checkbox": false,
            "icon": "api/files/static/file/SYSTEM/cxapp/viewCustomerIcons/20.svg"
        }
    }
}) AFTER Steps.if_Copy_1.false
                                    output
                                        columnsData: UIEngine.SendData(url = `Url.pathParts[1]  ? 'api/ui/personalization/cxapp/{{Store.auth.client.code}}customersColumns{{Url.pathParts[1]}}' :   'api/ui/personalization/cxapp/{{Store.auth.client.code}}customersColumns'`, method = "POST", payload =  Page.columns) AFTER Steps.setStore8.output