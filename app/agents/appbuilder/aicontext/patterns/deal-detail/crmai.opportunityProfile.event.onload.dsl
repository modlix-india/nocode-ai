FUNCTION onload
    LOGIC
        showLoadTrue: UIEngine.SetStore(path = "Page.showLoader", value = "showLoader")
            output
                readLeadData: _.readLeadData() AFTER Steps.showLoadTrue.output
                    output
                        gettingPipelineData: _.gettingPipelineData() AFTER Steps.readLeadData.output
                            output
                                generateStagesColors: _.generateStagesColors() AFTER Steps.gettingPipelineData.output
                                    output
                                        if: System.If(condition = Page.activeOppDetails.currentOwner.id) AFTER Steps.generateStagesColors.output
                                            true
                                                userDetailsGetting: hrms.getProfileWithIdWithoutClientCheck(userId = Page.activeOppDetails.currentOwner.id) AFTER Steps.if.true
                                                    output
                                                        set: UIEngine.SetStore(path = "Page.userDetails", value = Steps.userDetailsGetting.output.userProfile)
                                                            output
                                                                setStore: UIEngine.SetStore(path = "Page.activeTabButton", value = "activity") AFTER Steps.set.output
                        gettingActivityLogs: _.gettingActivityLogs() AFTER Steps.readLeadData.output
                            output
                                gettingNotesData: _.gettingNotesData() AFTER Steps.gettingActivityLogs.output
                                    output
                                        readTaskData: _.readTaskData() AFTER Steps.gettingNotesData.output
                                            output
                                                readingFileData: _.readingFileData() AFTER Steps.readTaskData.output
                                                    output
                                                        showLoadFalse: UIEngine.SetStore(path = "Page.showLoader", value = "stopLoader") AFTER Steps.readingFileData.output
                        checkingRole: _.checkAuthorization() AFTER Steps.readLeadData.output
                        setStore1: UIEngine.SetStore(path = "Page.status", value = Page.activeOppDetails.status) AFTER Steps.readLeadData.output
                            output
                                subStatus: _.subStatus() AFTER Steps.setStore1.output
                                    output
                                        setStore3: UIEngine.SetStore(path = "Page.subStatus", value = Page.activeOppDetails.subStatus) AFTER Steps.subStatus.output
                                            output
                                                setStore1_Copy_1: UIEngine.SetStore(path = "Page.tags", value = Page.activeOppDetails.tag) AFTER Steps.setStore3.output
                                                    output
                                                        setStore1_Copy_2: UIEngine.SetStore(path = "Page.closingDate", value = Page.activeOppDetails.closingDate) AFTER Steps.setStore1_Copy_1.output
        setStore4: UIEngine.SetStore(path = "Page.hoverIndex", value = -1)