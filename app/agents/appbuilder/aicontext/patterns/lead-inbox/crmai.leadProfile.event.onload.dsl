FUNCTION onload
    LOGIC
        showLoadTrue: UIEngine.SetStore(path = "Page.showLoader", value = "showLoader")
            output
                readLeadData: _.readLeadData() AFTER Steps.showLoadTrue.output
                    output
                        leadDataReading: _.checkLeadConvertedToOpp() AFTER Steps.readLeadData.output
                            output
                                dataOfOppFromStorageLength: System.If(condition = Page.dataOfOppFromStorage.length  >  0) AFTER Steps.leadDataReading.output
                                    true
                                        showButtonBadgeAfterConverted: UIEngine.SetStore(path = "Page.showBadge", value = "conformBadge") AFTER Steps.dataOfOppFromStorageLength.true
                                    false
                                        notConverted: UIEngine.SetStore(path = "Page.showBadge", value = "not_conformBadge") AFTER Steps.dataOfOppFromStorageLength.false
                        gettingActivityLogs: _.gettingActivityLogs() AFTER Steps.readLeadData.output
                            output
                                setStore: UIEngine.SetStore(path = "Page.activeTabButton", value = "activity") AFTER Steps.gettingActivityLogs.output
                                gettingNotesData: _.gettingNotesData() AFTER Steps.gettingActivityLogs.output
                                    output
                                        readTaskData: _.readTaskData() AFTER Steps.gettingNotesData.output
                                            output
                                                readingFileData: _.readingFileData() AFTER Steps.readTaskData.output
                                                    output
                                                        readingCallLogs: _.readingCallLogs() AFTER Steps.readingFileData.output
                                                            output
                                                                showLoadFalse: UIEngine.SetStore(path = "Page.showLoader", value = "stopLoader") AFTER Steps.readingCallLogs.output
                        checkAuthorization: _.checkAuthorization() AFTER Steps.readLeadData.output
                        if: System.If(condition = Page.activeLeadDetails.currentOwner.id) AFTER Steps.readLeadData.output
                            true
                                userDetailsGetting: hrms.getProfileWithIdWithoutClientCheck(userId = Page.activeLeadDetails.currentOwner.id) AFTER Steps.if.true
                                    output
                                        userDetails: UIEngine.SetStore(path = "Page.userDetails", value = Steps.userDetailsGetting.output.userProfile)
                        setStore1: UIEngine.SetStore(value = Page.activeLeadDetails.source, path = "Page.source") AFTER Steps.readLeadData.output
                            output
                                setStore3: UIEngine.SetStore(value = Page.activeLeadDetails.status, path = "Page.status") AFTER Steps.setStore1.output
        getUsers: _.getUsers()
        setStore4: UIEngine.SetStore(path = "Page.hoverIndex", value = -1)
        setStore5: UIEngine.SetStore(path = "Page.purpose", value = `"Welcome Call"`)