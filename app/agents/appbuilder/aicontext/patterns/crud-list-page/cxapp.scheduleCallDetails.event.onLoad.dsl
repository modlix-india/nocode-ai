FUNCTION onLoad
    LOGIC
        setSt4: UIEngine.SetStore(path = "Page.loader", value = true)
            output
                setStore23: UIEngine.SetStore(path = "Page.isAllcolumnTrue", value = true) AFTER Steps.setSt4.output
                    output
                        setSt2: UIEngine.SetStore(path = "Page.AllScheduleCalls.total", value = 1) AFTER Steps.setStore23.output
                            output
                                setSt3: UIEngine.SetStore(path = "Page.noScheduleCalls", value = true) AFTER Steps.setSt2.output
                                    output
                                        setStore11: UIEngine.SetStore(path = "Page.save", value = false) AFTER Steps.setSt3.output
                                            output
                                                setStore12: UIEngine.SetStore(path = "Page.calenderSelectionTypes", value = ["Today", "Yesterday", "Last week", "Last month", "Custom range"]) AFTER Steps.setStore11.output
                                                    output
                                                        setStore13: UIEngine.SetStore(path = "Page.projectSelectAll", value = true) AFTER Steps.setStore12.output
                                                            output
                                                                setStore14: UIEngine.SetStore(path = "Page.isCallTypeSelectAll", value = true) AFTER Steps.setStore13.output
                                                                    output
                                                                        setStore1_Copy_2: UIEngine.SetStore(path = "Page.filters", value = false) AFTER Steps.setStore14.output
                                                                            output
                                                                                setStore4: UIEngine.SetStore(path = "Page.projectNames", value = []) AFTER Steps.setStore1_Copy_2.output
                                                                                    output
                                                                                        setStore16: UIEngine.SetStore(path = "Page.projectNames[0].projectLength", value = Page.projectNames.length) AFTER Steps.setStore4.output
                                                                                            output
                                                                                                readPage: CoreServices.Storage.ReadPage(storageName = "Project", size = 500, appCode = "rim", filter = {
    "field": "projectFullName",
    "operator": "IS_NULL",
    "negate": true
}) AFTER Steps.setStore16.output
                                                                                                    output
                                                                                                        setStore2: UIEngine.SetStore(path = "Page.projects", value = Steps.readPage.output.result.content)
                                                                                                            output
                                                                                                                forEachLoop: System.Loop.ForEachLoop(source = Page.projects) AFTER Steps.setStore2.output
                                                                                                                    iteration
                                                                                                                        if1: System.If(condition = Page.projectNames[1].Page.projects[Steps.forEachLoop.iteration.index]._id)
                                                                                                                            false
                                                                                                                                setStore5: UIEngine.SetStore(path = `'Page.projectNames[1].{{Page.projects[Steps.forEachLoop.iteration.index]._id}}'`, value = {}) AFTER Steps.if1.false
                                                                                                                                    output
                                                                                                                                        setStore6: UIEngine.SetStore(path = `'Page.projectNames[1].{{Page.projects[{{Steps.forEachLoop.iteration.index}}]._id}}.projectName'`, value = Page.projects[{{Steps.forEachLoop.iteration.index}}].projectFullName) AFTER Steps.setStore5.output
                                                                                                                                            output
                                                                                                                                                setStore7: UIEngine.SetStore(path = `'Page.projectNames[1].{{Page.projects[Steps.forEachLoop.iteration.index]._id}}.checkbox'`, value = true) AFTER Steps.setStore6.output
                                                                                                                    output
                                                                                                                        setStore17: UIEngine.SetStore(path = "Page.projectNames[0].projectLength", value = Page.projectNames[1].length) AFTER Steps.forEachLoop.output
                                                                                                                            output
                                                                                                                                setStore18: UIEngine.SetStore(path = "Page.projectNamesLength", value = Page.projectNames[1].length) AFTER Steps.setStore17.output
                                                                                                                                    output
                                                                                                                                        objectKeys: System.Object.ObjectKeys(source = Page.projectNames[1]) AFTER Steps.setStore18.output
                                                                                                                                            output
                                                                                                                                                setStore1_Copy_1: UIEngine.SetStore(path = "Page.Allkeys", value = Steps.objectKeys.output.value)
                                                                                                                                                    output
                                                                                                                                                        setStore10: UIEngine.SetStore(path = "Page.filterObject", value = {
    "operator": "AND",
    "conditions": [
        {
            "field": "projectId",
            "operator": "IN",
            "multiValue": []
        },
        {
            "field": "callType",
            "operator": "IN",
            "multiValue": [
                "Telephonic call"
            ]
        }
    ]
}) AFTER Steps.setStore1_Copy_1.output
                                                                                                                                                            output
                                                                                                                                                                setStore3: UIEngine.SetStore(path = "Page.size", value = 10) AFTER Steps.setStore10.output
                                                                                                                                                                    output
                                                                                                                                                                        if: System.If(condition = Page.number = undefined) AFTER Steps.setStore3.output
                                                                                                                                                                            true
                                                                                                                                                                                setStore1: UIEngine.SetStore(path = "Page.number", value = 0) AFTER Steps.if.true
                                                                                                                                                                            output
                                                                                                                                                                                getAllCallDetails: hrms.getAllCallDetails(pageNumber = Page.number, size = Page.size??10, filterObject = Page.filterObject) AFTER Steps.if.output
                                                                                                                                                                                    output
                                                                                                                                                                                        setStore: UIEngine.SetStore(path = "Page.AllScheduleCalls", value = Steps.getAllCallDetails.output.scheduleCallDetails)
                                                                                                                                                                                            output
                                                                                                                                                                                                filterUniqueDates: _.filterUniqueDates() AFTER Steps.setStore.output
                                                                                                                                                                                                    output
                                                                                                                                                                                                        setStore8: UIEngine.SetStore(path = "Page.callTypes", value = [{
    "callTypeLength": 3
}, {
    "GoogleMeet": {
        "callType": "Google Meet",
        "checkbox": false
    },
    "ZoomMeet": {
        "callType": "Zoom Meet",
        "checkbox": false
    },
    "Telephoniccall": {
        "callType": "Telephonic call",
        "checkbox": true
    }
}]) AFTER Steps.filterUniqueDates.output
                                                                                                                                                                                                            output
                                                                                                                                                                                                                objectKeys1: System.Object.ObjectKeys(source = Page.callTypes[1]) AFTER Steps.setStore8.output
                                                                                                                                                                                                                    output
                                                                                                                                                                                                                        setStore9: UIEngine.SetStore(path = "Page.callTypekeys", value = Steps.objectKeys1.output.value)
                                                                                                                                                                                                                            output
                                                                                                                                                                                                                                currentDateFun: _.currentDateFun() AFTER Steps.setStore9.output
                                                                                                                                                                                                                                    output
                                                                                                                                                                                                                                        fetchingcolumnsData: _.fetchingcolumnsData() AFTER Steps.currentDateFun.output
                                                                                                                                                                                                                                            output
                                                                                                                                                                                                                                                if2: System.If(condition = Page.AllScheduleCalls.total  = 0) AFTER Steps.fetchingcolumnsData.output
                                                                                                                                                                                                                                                    true
                                                                                                                                                                                                                                                        setStore20: UIEngine.SetStore(path = "Page.noScheduleCalls", value = true) AFTER Steps.if2.true
                                                                                                                                                                                                                                                            output
                                                                                                                                                                                                                                                                setStore22: UIEngine.SetStore(path = "Page.noscheduleCallRecords", value = true) AFTER Steps.setStore20.output
                                                                                                                                                                                                                                                    false
                                                                                                                                                                                                                                                        setStore21: UIEngine.SetStore(path = "Page.noScheduleCalls", value = false) AFTER Steps.if2.false
                                                                                                                                                                                                                                                            output
                                                                                                                                                                                                                                                                setStore24: UIEngine.SetStore(path = "Page.noscheduleCallRecords", value = false) AFTER Steps.setStore21.output
                                                                                                                                                                                                                                                    output
                                                                                                                                                                                                                                                        setStore19: UIEngine.SetStore(path = "Page.loader", value = false) AFTER Steps.if2.output
                                                                                                                                                                                                                                                            output
                                                                                                                                                                                                                                                                setStore15: UIEngine.SetStore(path = "Page.showAllGrids", value = true) AFTER Steps.setStore19.output