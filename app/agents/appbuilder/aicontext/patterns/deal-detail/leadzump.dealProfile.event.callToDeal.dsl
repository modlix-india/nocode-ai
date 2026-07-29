FUNCTION callToDeal
    LOGIC
        callAnimation: UIEngine.SetStore(path = "Page.callAnimation", value = true)
            output
                callDeal: UIEngine.SetStore(path = "Page.callDeal", value = true) AFTER Steps.callAnimation.output
                    output
                        show: UIEngine.SetStore(path = "Page.show", value = "make call") AFTER Steps.callDeal.output
        setStore: UIEngine.SetStore(path = "Page.call.toNumber", value = Page.dealDetails.phoneNumber)
            output
                setStore1: UIEngine.SetStore(path = "Page.call.callerId", value = "<PHONE>") AFTER Steps.setStore.output
                    output
                        setStore1_Copy_1: UIEngine.SetStore(path = "Page.call.connectionName", value = "exotel_connection") AFTER Steps.setStore1.output
                            output
                                sendData: UIEngine.SendData(url = "api/message/call/make", method = "POST", payload = Page.call) AFTER Steps.setStore1_Copy_1.output