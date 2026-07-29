FUNCTION onLoad
    LOGIC
        setStore1: UIEngine.SetStore(path = "Page.dashboardData", value = [{
    "reportName": "Lead bucket report",
    "isSelected": false
}, {
    "reportName": "Call report",
    "isSelected": false
}, {
    "reportName": "Deal bucket report",
    "isSelected": false
}, {
    "reportName": "Funnel report",
    "isSelected": false
}, {
    "reportName": "Lead conversion report",
    "isSelected": false
}, {
    "reportName": "Deal conversion report",
    "isSelected": false
}, {
    "reportName": "Lead bucket report",
    "isSelected": false
}, {
    "reportName": "Call report",
    "isSelected": false
}, {
    "reportName": "Deal bucket report",
    "isSelected": false
}, {
    "reportName": "Funnel report",
    "isSelected": false
}, {
    "reportName": "Lead conversion report",
    "isSelected": false
}, {
    "reportName": "Deal conversion report",
    "isSelected": false
}])
        getBpConfigurationDetails: _.getBpConfigurationDetails()
        getProducts: _.getProducts()
            output
                onClickToggle: _.onClickToggle() AFTER Steps.getProducts.output
        setStore: UIEngine.SetStore(path = "Page.activeTab", value = `"Deals"`)