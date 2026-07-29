FUNCTION onLoad
    LOGIC
        setStore: UIEngine.SetStore(path = "Page.showLoader", value = "show")
            output
                getFilterData: _.getFilterData() AFTER Steps.setStore.output
                    output
                        setStore1: UIEngine.SetStore(path = "Page.showLoader", value = "dontShow") AFTER Steps.getFilterData.output
                        if: System.If(condition = Page.teammates.empty) AFTER Steps.getFilterData.output
                            true
                                setStore6: UIEngine.SetStore(path = "Page.showTeammates", deleteKey = true) AFTER Steps.if.true
                                    output
                                        emptyShowView: UIEngine.SetStore(path = "Page.show", value = "emptyView") AFTER Steps.setStore6.output
                            false
                                showTableView: UIEngine.SetStore(path = "Page.show", value = "tableView") AFTER Steps.if.false
        setStore12: UIEngine.SetStore(path = "Page.newFilterContent", value = [{
    "title": "Status",
    "img": "api/files/static/file/SYSTEM/leadzump/deals/statusFilterIcon.svg",
    "filterType": "status"
}])
            output
                setStore10: UIEngine.SetStore(path = "Page.newFilter.status", value = [{
    "isSelected": false,
    "name": "ACTIVE",
    "field": "status"
}, {
    "isSelected": false,
    "name": "INACTIVE",
    "field": "status"
}]) AFTER Steps.setStore12.output
                    output
                        setStore2: UIEngine.SetStore(path = "Page.activeFilter", value = `"status"`) AFTER Steps.setStore10.output
                            output
                                setStore3: UIEngine.SetStore(path = "Page.activeFilterData", value = Page.newFilter.status) AFTER Steps.setStore2.output
        setStore4: UIEngine.SetStore(path = "Page.active", value = "Status")