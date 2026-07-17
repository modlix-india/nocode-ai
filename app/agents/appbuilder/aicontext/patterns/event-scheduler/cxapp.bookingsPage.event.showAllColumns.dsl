FUNCTION showAllColumns
    LOGIC
        if: System.If(condition = Page.filterColumns.allColumns)
            true
                if1: System.If(condition = Page.isCommercial = true) AFTER Steps.if.true
                    true
                        setStore8: UIEngine.SetStore(path = "Page.filterColumns", value = {
    "allColumns": true,
    "data": {
        "AccountType": {
            "color": "#FFC200",
            "backgroundColor": "#DBA9791A",
            "name": "Account Type",
            "checkbox": true,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/Group<PHONE>.svg"
        },
        "AmountInvested": {
            "color": "#03AED2",
            "backgroundColor": "#DBA9791A",
            "name": "Amount Invested",
            "checkbox": true,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/Group<PHONE>.svg"
        },
        "AmountPaid": {
            "color": "#03AED2",
            "backgroundColor": "#DBA9791A",
            "name": "Amount Paid",
            "checkbox": true,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/Group<PHONE>.svg"
        },
        "AmountPending": {
            "color": "#03AED2",
            "backgroundColor": "#DBA9791A",
            "name": "Amount Pending",
            "checkbox": true,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/Group<PHONE>.svg"
        },
        "BaseRate": {
            "color": "#D8B4F8",
            "backgroundColor": "#DBA9791A",
            "name": "Base Rate",
            "checkbox": true,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/Group<PHONE>.svg"
        },
        "BookingDate": {
            "color": "#3fbf86",
            "backgroundColor": "#DBA9791A",
            "name": "Booking Date",
            "checkbox": true,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/Group<PHONE>.svg"
        },
        "BookingMonth": {
            "color": "#ddaf84",
            "backgroundColor": "#DBA9791A",
            "name": "Booking Month",
            "checkbox": true,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/Group<PHONE>.svg"
        },
        "BookingNames": {
            "color": "#FFC200",
            "backgroundColor": "#DBA9791A",
            "name": "Booking Names",
            "checkbox": true,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/Group<PHONE>.svg"
        },
        "Ownership": {
            "color": "#FFC200",
            "backgroundColor": "#DBA9791A",
            "name": "Ownership",
            "checkbox": true,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/Group<PHONE>.svg"
        },
        "CarpetArea": {
            "color": "#E672AB",
            "backgroundColor": "#DBA9791A",
            "name": "Carpet Area",
            "checkbox": true,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/Group<PHONE>.svg"
        },
        "GST": {
            "color": "#FF0000",
            "backgroundColor": "#DBA9791A",
            "name": "GST",
            "checkbox": true,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/Group<PHONE>.svg"
        },
        "ProjectName": {
            "color": "#FFC200",
            "backgroundColor": "#DBA9791A",
            "name": "Project Name",
            "checkbox": true,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/Group<PHONE>.svg"
        },
        "PropCommArea": {
            "color": "#FFBB70",
            "backgroundColor": "#DBA9791A",
            "name": "Prop Comm Area",
            "checkbox": true,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/Group<PHONE>.svg"
        },
        "Rate": {
            "color": "#03AED2",
            "backgroundColor": "#DBA9791A",
            "name": "Rate",
            "checkbox": true,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/Group<PHONE>.svg"
        },
        "SalesManager": {
            "color": "#D8B4F8",
            "backgroundColor": "#DBA9791A",
            "name": "Sales Manager",
            "checkbox": true,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/Group<PHONE>.svg"
        },
        "SBArea": {
            "color": "#FFC200",
            "backgroundColor": "#DBA9791A",
            "name": "SB Area",
            "checkbox": true,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/Group<PHONE>.svg"
        },
        "StampDuty": {
            "color": "#FFBB70",
            "backgroundColor": "#DBA9791A",
            "name": "Stamp Duty",
            "checkbox": true,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/Group<PHONE>.svg"
        },
        "Status": {
            "color": "#FFBB70",
            "backgroundColor": "#DBA9791A",
            "name": "Status",
            "checkbox": true,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/Group<PHONE>.svg"
        },
        "Taxable": {
            "color": "#D8B4F8",
            "backgroundColor": "#DBA9791A",
            "name": "Taxable",
            "checkbox": true,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/Group<PHONE>.svg"
        },
        "ProjectDocument": {
            "color": "#6AD4DD",
            "backgroundColor": "#DBA9791A",
            "name": "Project Document",
            "checkbox": true,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/Group<PHONE>.svg"
        }
    }
}) AFTER Steps.if1.true
                    false
                        setStore4: UIEngine.SetStore(path = "Page.filterColumns", value = {
    "allColumns": true,
    "data": {
        "AccountType": {
            "color": "#FFC200",
            "backgroundColor": "#DBA9791A",
            "name": "Account Type",
            "checkbox": true,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/residentialColumnIcon/accountType.svg"
        },
        "AmountPaid": {
            "color": "#03AED2",
            "backgroundColor": "#DBA9791A",
            "name": "Amount Paid",
            "checkbox": true,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/residentialColumnIcon/Group<PHONE>.svg"
        },
        "AmountPending": {
            "color": "#03AED2",
            "backgroundColor": "#DBA9791A",
            "name": "Amount Pending",
            "checkbox": true,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/residentialColumnIcon/Group<PHONE>.svg"
        },
        "TotalUnitcost": {
            "color": "#D8B4F8",
            "backgroundColor": "#DBA9791A",
            "name": "Total Unit Cost(GST)",
            "checkbox": true,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/residentialColumnIcon/Group<PHONE>.svg"
        },
        "BookingDate": {
            "color": "#3fbf86",
            "backgroundColor": "#DBA9791A",
            "name": "Booking Date",
            "checkbox": true,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/residentialColumnIcon/Group<PHONE>.svg"
        },
        "BookingMonth": {
            "color": "#ddaf84",
            "backgroundColor": "#DBA9791A",
            "name": "Booking Month",
            "checkbox": true,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/residentialColumnIcon/Group<PHONE>.svg"
        },
        "BookingNames": {
            "color": "#FFC200",
            "backgroundColor": "#DBA9791A",
            "name": "Booking Names",
            "checkbox": true,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/residentialColumnIcon/Group<PHONE>.svg"
        },
        "Ownership": {
            "color": "#FFC200",
            "backgroundColor": "#DBA9791A",
            "name": "Ownership",
            "checkbox": true,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/Group<PHONE>.svg"
        },
        "ProjectName": {
            "color": "#FFC200",
            "backgroundColor": "#DBA9791A",
            "name": "Project Name",
            "checkbox": true,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/residentialColumnIcon/Group<PHONE>.svg"
        },
        "SalesManager": {
            "color": "#D8B4F8",
            "backgroundColor": "#DBA9791A",
            "name": "Sales Manager",
            "checkbox": true,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/residentialColumnIcon/Group<PHONE>.svg"
        },
        "Status": {
            "color": "#FFBB70",
            "backgroundColor": "#DBA9791A",
            "name": "Status",
            "checkbox": true,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/residentialColumnIcon/Group<PHONE>+%281%29.svg"
        },
        "ProjectDocument": {
            "color": "#6AD4DD",
            "backgroundColor": "#DBA9791A",
            "name": "Project Document",
            "checkbox": true,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/residentialColumnIcon/Group<PHONE>+%281%29.svg"
        },
        "UnitNumber": {
            "color": "#6AD4DD",
            "backgroundColor": "#DBA9791A",
            "name": "Unit Number",
            "checkbox": true,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/Group<PHONE>.svg"
        },
        "FloorNumber": {
            "color": "#6AD4DD",
            "backgroundColor": "#DBA9791A",
            "name": "Floor Number",
            "checkbox": true,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/Group<PHONE>.svg"
        },
        "UnitType": {
            "color": "#6AD4DD",
            "backgroundColor": "#DBA9791A",
            "name": "Unit Type",
            "checkbox": true,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/residentialColumnIcon/Group<PHONE>.svg"
        },
        "UnitConfiguration": {
            "color": "#6AD4DD",
            "backgroundColor": "#DBA9791A",
            "name": "Unit Configuration",
            "checkbox": true,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/residentialColumnIcon/Group<PHONE>.svg"
        },
        "SuperBuiltUpArea": {
            "color": "#6AD4DD",
            "backgroundColor": "#DBA9791A",
            "name": "Super Built-up Area",
            "checkbox": true,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/residentialColumnIcon/Group<PHONE>.svg"
        },
        "Facing": {
            "color": "#6AD4DD",
            "backgroundColor": "#DBA9791A",
            "name": "Facing",
            "checkbox": true,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/residentialColumnIcon/Group<PHONE>.svg"
        },
        "PhaseName": {
            "color": "#6AD4DD",
            "backgroundColor": "#DBA9791A",
            "name": "Phase Name",
            "checkbox": true,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/residentialColumnIcon/Group<PHONE>.svg"
        },
        "TowerName": {
            "color": "#6AD4DD",
            "backgroundColor": "#DBA9791A",
            "name": "Tower Name",
            "checkbox": true,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/residentialColumnIcon/Group<PHONE>.svg"
        },
        "BlockName": {
            "color": "#6AD4DD",
            "backgroundColor": "#DBA9791A",
            "name": "Block Name",
            "checkbox": true,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/residentialColumnIcon/Group<PHONE>.svg"
        },
        "BookingType": {
            "color": "#6AD4DD",
            "backgroundColor": "#DBA9791A",
            "name": "Booking Type",
            "checkbox": true,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/residentialColumnIcon/Group<PHONE>.svg"
        },
        "CarParking": {
            "color": "#6AD4DD",
            "backgroundColor": "#DBA9791A",
            "name": "Car Parking",
            "checkbox": true,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/residentialColumnIcon/Group<PHONE>.svg"
        },
        "GardenAndTerraceArea": {
            "color": "#6AD4DD",
            "backgroundColor": "#DBA9791A",
            "name": "Garden and Terrace Area",
            "checkbox": true,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/residentialColumnIcon/Group<PHONE>.svg"
        },
        "SaleableArea": {
            "color": "#6AD4DD",
            "backgroundColor": "#DBA9791A",
            "name": "Saleable Area",
            "checkbox": true,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/residentialColumnIcon/Group<PHONE>.svg"
        },
        "FloorPlanType": {
            "color": "#6AD4DD",
            "backgroundColor": "#DBA9791A",
            "name": "Floor Plan Type",
            "checkbox": true,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/residentialColumnIcon/Group<PHONE>.svg"
        },
        "PlotType": {
            "color": "#6AD4DD",
            "backgroundColor": "#DBA9791A",
            "name": "Plot Type",
            "checkbox": true,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/residentialColumnIcon/Group<PHONE>.svg"
        },
        "PlotDimension": {
            "color": "#6AD4DD",
            "backgroundColor": "#DBA9791A",
            "name": "Plot Dimension",
            "checkbox": true,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/residentialColumnIcon/Group<PHONE>.svg"
        },
        "SurveyNumber": {
            "color": "#6AD4DD",
            "backgroundColor": "#DBA9791A",
            "name": "Survey Number",
            "checkbox": true,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/residentialColumnIcon/Group<PHONE>.svg"
        },
        "Action": {
            "color": "#6AD4DD",
            "backgroundColor": "#DBA9791A",
            "name": "Action",
            "checkbox": true,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/residentialColumnIcon/Group<PHONE>+%281%29.svg"
        }
    }
}) AFTER Steps.if1.false
                    output
                        setStore1: UIEngine.SetStore(path = "Page.isAllcolumnTrue", value = true) AFTER Steps.if1.output
                            output
                                setStore2: UIEngine.SetStore(path = "Page.columns", value = Page.filterColumns) AFTER Steps.setStore1.output
            false
                if2: System.If(condition = Page.isCommercial = true) AFTER Steps.if.false
                    true
                        setStore8_Copy_1: UIEngine.SetStore(path = "Page.filterColumns", value = {
    "allColumns": false,
    "data": {
        "AccountType": {
            "color": "#FFC200",
            "backgroundColor": "#DBA9791A",
            "name": "Account Type",
            "checkbox": false,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/Group<PHONE>.svg"
        },
        "AmountInvested": {
            "color": "#03AED2",
            "backgroundColor": "#DBA9791A",
            "name": "Amount Invested",
            "checkbox": false,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/Group<PHONE>.svg"
        },
        "AmountPaid": {
            "color": "#03AED2",
            "backgroundColor": "#DBA9791A",
            "name": "Amount Paid",
            "checkbox": false,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/Group<PHONE>.svg"
        },
        "AmountPending": {
            "color": "#03AED2",
            "backgroundColor": "#DBA9791A",
            "name": "Amount Pending",
            "checkbox": false,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/Group<PHONE>.svg"
        },
        "BaseRate": {
            "color": "#D8B4F8",
            "backgroundColor": "#DBA9791A",
            "name": "Base Rate",
            "checkbox": false,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/Group<PHONE>.svg"
        },
        "BookingDate": {
            "color": "#3fbf86",
            "backgroundColor": "#DBA9791A",
            "name": "Booking Date",
            "checkbox": false,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/Group<PHONE>.svg"
        },
        "BookingMonth": {
            "color": "#ddaf84",
            "backgroundColor": "#DBA9791A",
            "name": "Booking Month",
            "checkbox": false,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/Group<PHONE>.svg"
        },
        "BookingNames": {
            "color": "#FFC200",
            "backgroundColor": "#DBA9791A",
            "name": "Booking Names",
            "checkbox": false,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/Group<PHONE>.svg"
        },
        "Ownership": {
            "color": "#FFC200",
            "backgroundColor": "#DBA9791A",
            "name": "Ownership",
            "checkbox": false,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/Group<PHONE>.svg"
        },
        "CarpetArea": {
            "color": "#E672AB",
            "backgroundColor": "#DBA9791A",
            "name": "Carpet Area",
            "checkbox": false,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/Group<PHONE>.svg"
        },
        "GST": {
            "color": "#FF0000",
            "backgroundColor": "#DBA9791A",
            "name": "GST",
            "checkbox": false,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/Group<PHONE>.svg"
        },
        "ProjectName": {
            "color": "#FFC200",
            "backgroundColor": "#DBA9791A",
            "name": "Project Name",
            "checkbox": false,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/Group<PHONE>.svg"
        },
        "PropCommArea": {
            "color": "#FFBB70",
            "backgroundColor": "#DBA9791A",
            "name": "Prop Comm Area",
            "checkbox": false,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/Group<PHONE>.svg"
        },
        "Rate": {
            "color": "#03AED2",
            "backgroundColor": "#DBA9791A",
            "name": "Rate",
            "checkbox": false,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/Group<PHONE>.svg"
        },
        "SalesManager": {
            "color": "#D8B4F8",
            "backgroundColor": "#DBA9791A",
            "name": "Sales Manager",
            "checkbox": false,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/Group<PHONE>.svg"
        },
        "SBArea": {
            "color": "#FFC200",
            "backgroundColor": "#DBA9791A",
            "name": "SB Area",
            "checkbox": false,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/Group<PHONE>.svg"
        },
        "StampDuty": {
            "color": "#FFBB70",
            "backgroundColor": "#DBA9791A",
            "name": "Stamp Duty",
            "checkbox": false,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/Group<PHONE>.svg"
        },
        "Status": {
            "color": "#FFBB70",
            "backgroundColor": "#DBA9791A",
            "name": "Status",
            "checkbox": false,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/Group<PHONE>.svg"
        },
        "Taxable": {
            "color": "#D8B4F8",
            "backgroundColor": "#DBA9791A",
            "name": "Taxable",
            "checkbox": false,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/Group<PHONE>.svg"
        },
        "ProjectDocument": {
            "color": "#6AD4DD",
            "backgroundColor": "#DBA9791A",
            "name": "Project Document",
            "checkbox": false,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/Group<PHONE>.svg"
        }
    }
}) AFTER Steps.if2.true
                    false
                        setStore5: UIEngine.SetStore(path = "Page.filterColumns", value = {
    "allColumns": false,
    "data": {
        "AccountType": {
            "color": "#FFC200",
            "backgroundColor": "#DBA9791A",
            "name": "Account Type",
            "checkbox": false,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/residentialColumnIcon/accountType.svg"
        },
        "AmountPaid": {
            "color": "#03AED2",
            "backgroundColor": "#DBA9791A",
            "name": "Amount Paid",
            "checkbox": false,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/residentialColumnIcon/Group<PHONE>.svg"
        },
        "AmountPending": {
            "color": "#03AED2",
            "backgroundColor": "#DBA9791A",
            "name": "Amount Pending",
            "checkbox": false,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/residentialColumnIcon/Group<PHONE>.svg"
        },
        "TotalUnitcost": {
            "color": "#D8B4F8",
            "backgroundColor": "#DBA9791A",
            "name": "Total Unit Cost(GST)",
            "checkbox": false,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/residentialColumnIcon/Group<PHONE>.svg"
        },
        "BookingDate": {
            "color": "#3fbf86",
            "backgroundColor": "#DBA9791A",
            "name": "Booking Date",
            "checkbox": false,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/residentialColumnIcon/Group<PHONE>.svg"
        },
        "BookingMonth": {
            "color": "#ddaf84",
            "backgroundColor": "#DBA9791A",
            "name": "Booking Month",
            "checkbox": false,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/residentialColumnIcon/Group<PHONE>.svg"
        },
        "BookingNames": {
            "color": "#FFC200",
            "backgroundColor": "#DBA9791A",
            "name": "Booking Names",
            "checkbox": false,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/residentialColumnIcon/Group<PHONE>.svg"
        },
        "Ownership": {
            "color": "#FFC200",
            "backgroundColor": "#DBA9791A",
            "name": "Ownership",
            "checkbox": false,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/Group<PHONE>.svg"
        },
        "ProjectName": {
            "color": "#FFC200",
            "backgroundColor": "#DBA9791A",
            "name": "Project Name",
            "checkbox": false,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/residentialColumnIcon/Group<PHONE>.svg"
        },
        "SalesManager": {
            "color": "#D8B4F8",
            "backgroundColor": "#DBA9791A",
            "name": "Sales Manager",
            "checkbox": false,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/residentialColumnIcon/Group<PHONE>.svg"
        },
        "Status": {
            "color": "#FFBB70",
            "backgroundColor": "#DBA9791A",
            "name": "Status",
            "checkbox": false,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/residentialColumnIcon/Group<PHONE>+%281%29.svg"
        },
        "ProjectDocument": {
            "color": "#6AD4DD",
            "backgroundColor": "#DBA9791A",
            "name": "Project Document",
            "checkbox": false,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/residentialColumnIcon/Group<PHONE>+%281%29.svg"
        },
        "UnitNumber": {
            "color": "#6AD4DD",
            "backgroundColor": "#DBA9791A",
            "name": "Unit Number",
            "checkbox": false,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/Group<PHONE>.svg"
        },
        "FloorNumber": {
            "color": "#6AD4DD",
            "backgroundColor": "#DBA9791A",
            "name": "Floor Number",
            "checkbox": false,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/Group<PHONE>.svg"
        },
        "UnitType": {
            "color": "#6AD4DD",
            "backgroundColor": "#DBA9791A",
            "name": "Unit Type",
            "checkbox": false,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/residentialColumnIcon/Group<PHONE>.svg"
        },
        "UnitConfiguration": {
            "color": "#6AD4DD",
            "backgroundColor": "#DBA9791A",
            "name": "Unit Configuration",
            "checkbox": false,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/residentialColumnIcon/Group<PHONE>.svg"
        },
        "SuperBuiltUpArea": {
            "color": "#6AD4DD",
            "backgroundColor": "#DBA9791A",
            "name": "Super Built-up Area",
            "checkbox": false,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/residentialColumnIcon/Group<PHONE>.svg"
        },
        "Facing": {
            "color": "#6AD4DD",
            "backgroundColor": "#DBA9791A",
            "name": "Facing",
            "checkbox": false,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/residentialColumnIcon/Group<PHONE>.svg"
        },
        "PhaseName": {
            "color": "#6AD4DD",
            "backgroundColor": "#DBA9791A",
            "name": "Phase Name",
            "checkbox": false,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/residentialColumnIcon/Group<PHONE>.svg"
        },
        "TowerName": {
            "color": "#6AD4DD",
            "backgroundColor": "#DBA9791A",
            "name": "Tower Name",
            "checkbox": false,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/residentialColumnIcon/Group<PHONE>.svg"
        },
        "BlockName": {
            "color": "#6AD4DD",
            "backgroundColor": "#DBA9791A",
            "name": "Block Name",
            "checkbox": false,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/residentialColumnIcon/Group<PHONE>.svg"
        },
        "BookingType": {
            "color": "#6AD4DD",
            "backgroundColor": "#DBA9791A",
            "name": "Booking Type",
            "checkbox": false,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/residentialColumnIcon/Group<PHONE>.svg"
        },
        "CarParking": {
            "color": "#6AD4DD",
            "backgroundColor": "#DBA9791A",
            "name": "Car Parking",
            "checkbox": false,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/residentialColumnIcon/Group<PHONE>.svg"
        },
        "GardenAndTerraceArea": {
            "color": "#6AD4DD",
            "backgroundColor": "#DBA9791A",
            "name": "Garden and Terrace Area",
            "checkbox": false,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/residentialColumnIcon/Group<PHONE>.svg"
        },
        "SaleableArea": {
            "color": "#6AD4DD",
            "backgroundColor": "#DBA9791A",
            "name": "Saleable Area",
            "checkbox": false,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/residentialColumnIcon/Group<PHONE>.svg"
        },
        "FloorPlanType": {
            "color": "#6AD4DD",
            "backgroundColor": "#DBA9791A",
            "name": "Floor Plan Type",
            "checkbox": false,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/residentialColumnIcon/Group<PHONE>.svg"
        },
        "PlotType": {
            "color": "#6AD4DD",
            "backgroundColor": "#DBA9791A",
            "name": "Plot Type",
            "checkbox": false,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/residentialColumnIcon/Group<PHONE>.svg"
        },
        "PlotDimension": {
            "color": "#6AD4DD",
            "backgroundColor": "#DBA9791A",
            "name": "Plot Dimension",
            "checkbox": false,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/residentialColumnIcon/Group<PHONE>.svg"
        },
        "SurveyNumber": {
            "color": "#6AD4DD",
            "backgroundColor": "#DBA9791A",
            "name": "Survey Number",
            "checkbox": false,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/residentialColumnIcon/Group<PHONE>.svg"
        },
        "Action": {
            "color": "#6AD4DD",
            "backgroundColor": "#DBA9791A",
            "name": "Action",
            "checkbox": false,
            "icon": "api/files/static/file/SYSTEM/cxapp/bookingsPage/residentialColumnIcon/Group<PHONE>+%281%29.svg"
        }
    }
}, deleteKey = ``) AFTER Steps.if2.false
                    output
                        setStore: UIEngine.SetStore(path = "Page.isAllcolumnTrue", value = false) AFTER Steps.if2.output
                            output
                                setStore3: UIEngine.SetStore(path = "Page.columns", value = Page.filterColumns) AFTER Steps.setStore.output