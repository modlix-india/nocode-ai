FUNCTION onLoad
    LOGIC
        activeIndex: UIEngine.SetStore(path = "Page.activeIndex", value = -1)
        phonenumber: UIEngine.SetStore(path = "Page.showphonenumber", value = true)
        sourcevalues: UIEngine.SetStore(path = "Page.sourcevalues", value = ["Direct", "Social Media", "Walk-In", "Channel Partner", "Referral", "Portal Leads", "Property Expo", "B2C Application", "Individual Financial", "Advisor", "Phone Number", "Add Contact"])
        socialmediavalues: UIEngine.SetStore(path = "Page.socialmediavalues", value = ["Facebook", "Instagram", "Google PPC", "Website Phone", "Facebook Forms", "Google Lead Form", "Website Form", "LinkedIn Forms", "Phone Number"])
        portalvalues: UIEngine.SetStore(path = "Page.portalvalues", value = ["Housing.com", "CommonFloor", "99Acres", "MagicBricks"])
        dataDummy: UIEngine.SetStore(path = "Page.fields", value = [{
    "field": "GT Towers"
}, {
    "field": "Shriram Hebbal"
}, {
    "field": "Prestige Homes"
}, {
    "field": "Mahaveer Space"
}])
        readInitiallyStorageData_copy: _.readInitiallyStorageData_copy()
            output
                if: System.If(condition = Page.clientDetails.length = 0 ) AFTER Steps.readInitiallyStorageData_copy.output
                    true
                        checkingExotel_IfNot: UIEngine.SetStore(path = "Page.exotelSelect", value = false) AFTER Steps.if.true
                    false
                        checkingExotel: UIEngine.SetStore(path = "Page.exotelSelect", value = true) AFTER Steps.if.false
                    output
                        sellectInitialPage: UIEngine.SetStore(path = "Page.showPage", value = "selectIntial") AFTER Steps.if.output