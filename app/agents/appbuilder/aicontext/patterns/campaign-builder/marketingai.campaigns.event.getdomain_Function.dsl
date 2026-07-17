FUNCTION getdomain_Function
    LOGIC
        getAppUrl: CoreServices.Security.GetAppUrl(clientCode = Store.auth.loggedInClientCode, appCode = Store.application.appCode)
            output
                setStore: UIEngine.SetStore(path = "Page.domainUrl", value = Steps.getAppUrl.output.result)
                    output
                        concatenate: System.String.Concatenate(value = Page.domainUrl, value = `'/'`, value = Store.urlDetails.pathParts[0]) AFTER Steps.setStore.output
                            output
                                setStore1: UIEngine.SetStore(path = "Page.notifyUrl", value = Steps.concatenate.output.value)