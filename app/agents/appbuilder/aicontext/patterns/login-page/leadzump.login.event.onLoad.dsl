FUNCTION onLoad
    LOGIC
        setStore1: UIEngine.SetStore(path = "Page.activeTab", value = "SignIn")
            output
                setStore: UIEngine.SetStore(path = "Page.currentState", value = 4) AFTER Steps.setStore1.output
                    output
                        setStore3: UIEngine.SetStore(path = "Page.showMainGrid", value = `"showMain"`) AFTER Steps.setStore.output
        setStore2: UIEngine.SetStore(path = "Page.showClientsPopup", value = `false`)
        timeInSec: UIEngine.SetStore(path = "Page.time", value = 59)
        socialOnload: Authzump.sso.SocialOnload(userName = Store.urlDetails.queryParameters.emailId)
            output
                if: System.If(condition = Steps.socialOnload.output.multiUser)
                    true
                        setStore2_Copy_1: UIEngine.SetStore(path = "Page.IsClientsPresent", value = true) AFTER Steps.if.true
                            output
                                setStore1_Copy_2: UIEngine.SetStore(path = "Page.usersList", value = Steps.socialOnload.output.data) AFTER Steps.setStore2_Copy_1.output
                                    output
                                        setStore2_Copy_1_Copy_1: UIEngine.SetStore(path = "Page.showClientsPopup", value = `true`) AFTER Steps.setStore1_Copy_2.output