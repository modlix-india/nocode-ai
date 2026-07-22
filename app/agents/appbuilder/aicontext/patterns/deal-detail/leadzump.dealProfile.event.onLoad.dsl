FUNCTION onLoad
    LOGIC
        hoverIndex: UIEngine.SetStore(path = "Page.hoverIndex", value = -1)
        setStore6: UIEngine.SetStore(path = "Page.registrationPopup", value = false)
        setStore16: UIEngine.SetStore(path = "Page.viewChat", value = `false`)
        setStore_Copy_1_Copy_1: UIEngine.SetStore(path = "Page.subConnection", value = "WHATSAPP")
            output
                fetch: UIEngine.FetchData(url = "/api/core/connections", queryParams = {
    "appCode": {
        "location": {
            "type": "EXPRESSION",
            "expression": "Store.application.appCode"
        }
    },
    "clientCode": {
        "location": {
            "type": "EXPRESSION",
            "expression": "Store.auth.client.code"
        }
    },
    "connectionSubType": {
        "location": {
            "type": "EXPRESSION",
            "expression": "Page.subConnection"
        }
    }
}) AFTER Steps.setStore_Copy_1_Copy_1.output
                    output
                        setStore_Copy_2: UIEngine.SetStore(path = "Page.connections", value = Steps.fetch.output.data.content)
                            output
                                setStore_Copy_1_Copy_2: UIEngine.SetStore(path = "Page.subConnection", value = "EXOTEL") AFTER Steps.setStore_Copy_2.output
                                    output
                                        fetch1: UIEngine.FetchData(url = "/api/core/connections", queryParams = {
    "appCode": {
        "location": {
            "type": "EXPRESSION",
            "expression": "Store.application.appCode"
        }
    },
    "clientCode": {
        "location": {
            "type": "EXPRESSION",
            "expression": "Store.auth.client.code"
        }
    },
    "connectionSubType": {
        "location": {
            "type": "EXPRESSION",
            "expression": "Page.subConnection"
        }
    }
}) AFTER Steps.setStore_Copy_1_Copy_2.output
                                            output
                                                setStore_Copy_3: UIEngine.SetStore(path = "Page.exotelConnections", value = Steps.fetch1.output.data.content)
        setStore17: UIEngine.SetStore(path = "Page.size", value = 10)
        heightInitial: UIEngine.SetStore(path = "Page.viewHeightDCRM", value = `"close"`) /* for initially we can see logs. so prividing height .
 */
        setStore_Copy_1: UIEngine.SetStore(path = "Page.activePageNo", value = `0`)
        setStore1: UIEngine.SetStore(path = "Page.mailEdit", value = `false`)
            output
                setStore2: UIEngine.SetStore(path = "Page.loader", value = true) AFTER Steps.setStore1.output
                    output
                        activeTab: UIEngine.SetStore(path = "Page.activeTab", value = "Overview") AFTER Steps.setStore2.output
                            output
                                getDealDetails: _.getDealDetails() AFTER Steps.activeTab.output
                                    output
                                        if_Copy_1: System.If(condition = `Page.hasDealAccess = "Yes"`) AFTER Steps.getDealDetails.output
                                            true
                                                setStore5: UIEngine.SetStore(path = "Page.form.emailId", value = Page.dealDetails.email) AFTER Steps.if_Copy_1.true
                                                    output
                                                        setStore7: UIEngine.SetStore(path = "Page.form.phoneNumber", value = Page.dealDetails.phoneNumber) AFTER Steps.setStore5.output
                                                            output
                                                                setStore1_Copy_1: UIEngine.SetStore(path = "Page.user.userName", value = Page.form.emailId) AFTER Steps.setStore7.output
                                                                    output
                                                                        setStore1_Copy_1_Copy_2: UIEngine.SetStore(path = "Page.user.identifierType", value = "EMAIL_ID") AFTER Steps.setStore1_Copy_1.output
                                                                            output
                                                                                if5: System.If(condition = Page.form.emailId) AFTER Steps.setStore1_Copy_1_Copy_2.output
                                                                                    true
                                                                                        if4: System.If(condition = Page.productConnectionDetails.content[0].connectionName != undefined) AFTER Steps.if5.true
                                                                                            true
                                                                                                findUserClients: leadzump.findUserClients(payload = Page.user, connectionName = Page.productConnectionDetails.content[0].connectionName) AFTER Steps.if4.true
                                                                                                    output
                                                                                                        setStore8: UIEngine.SetStore(path = "Page.customer", value = Steps.findUserClients.output.response)
                                                                                            output
                                                                                                setStore10: UIEngine.SetStore(path = "Page.custId", value = Page.customer[0].userId) AFTER Steps.if4.output
                                                                                                    output
                                                                                                        if: System.If(condition = Page.customer.length !=0) AFTER Steps.setStore10.output
                                                                                                            true
                                                                                                                setStore9: UIEngine.SetStore(path = "Page.sendInvite", value = "show") AFTER Steps.if.true
                                                                                                            false
                                                                                                                setStore9_Copy_1: UIEngine.SetStore(path = "Page.sendInvite", value = "not show") AFTER Steps.if.false
                                                                                                                    output
                                                                                                                        setStore1_Copy_2: UIEngine.SetStore(path = "Page.mailEdit", value = `true`) AFTER Steps.setStore9_Copy_1.output
                                                                                    output
                                                                                        setStore15: UIEngine.SetStore(path = "Page.bookingDetails", value = []) AFTER Steps.if5.output
                                                                                            output
                                                                                                if1: System.If(condition = Url.pathParts[2]) AFTER Steps.setStore15.output
                                                                                                    true
                                                                                                        setStore11: UIEngine.SetStore(path = "Page.activeTab", value = "Sales") AFTER Steps.if1.true
                                                                                                            output
                                                                                                                getBookingsInSalesTab: _.getBookingsInSalesTab() AFTER Steps.setStore11.output
                                                                                                                    output
                                                                                                                        setStore: UIEngine.SetStore(path = "Page.showDefaultScreen", value = true) AFTER Steps.getBookingsInSalesTab.output
                                                                                                                            output
                                                                                                                                setStore13: UIEngine.SetStore(path = "Page.loader", value = false) AFTER Steps.setStore.output
                                                                                                                                    output
                                                                                                                                        getCurrentMillsecond: _.getCurrentMillsecond() AFTER Steps.setStore13.output
                                                                                                                                            output
                                                                                                                                                if3: System.If(condition = {{Url.pathParts[2]}} > Page.currentTimeMillisec) AFTER Steps.getCurrentMillsecond.output
                                                                                                                                                    true
                                                                                                                                                        if6: System.If(condition = `Url.pathParts[3] and Url.pathParts[3] = 'EOI'`) AFTER Steps.if3.true
                                                                                                                                                            true
                                                                                                                                                                setStore18: UIEngine.SetStore(path = "Page.blockingSuccessMessage", value = "EOI has been shared successfully") AFTER Steps.if6.true
                                                                                                                                                            false
                                                                                                                                                                setStore18_Copy_1: UIEngine.SetStore(path = "Page.blockingSuccessMessage", value = "Cost sheet has been sent successfully") AFTER Steps.if6.false
                                                                                                                                                            output
                                                                                                                                                                setStore4: UIEngine.SetStore(path = "Page.unitSuccesMessagePopup", value = true) AFTER Steps.if6.output
                                                                                                                                                                    output
                                                                                                                                                                        wait: System.Wait(millis = 4000) AFTER Steps.setStore4.output
                                                                                                                                                                            output
                                                                                                                                                                                setStore14: UIEngine.SetStore(path = "Page.unitSuccesMessagePopup", value = false) AFTER Steps.wait.output
                                                                                                    false
                                                                                                        setStore12: UIEngine.SetStore(path = "Page.unitSuccesMessagePopup", value = false) AFTER Steps.if1.false
                                                                                                    output
                                                                                                        setStore3: UIEngine.SetStore(path = "Page.loader", value = false) AFTER Steps.if1.output
                                                breadcrumb_url: UIEngine.SetStore(path = "Page.initialBreadCrumb.url", value = `"api/files/static/_withInSubClient/products/"+Page.dealDetails.productId.id`) AFTER Steps.if_Copy_1.true
                                                    output
                                                        breadcrumb_url_Copy_1: UIEngine.SetStore(path = "Page.initialBreadCrumb.name", value = `"Home"`) AFTER Steps.breadcrumb_url.output
                                                            output
                                                                setStore3_Copy_1: UIEngine.SetStore(path = "Page.breadcrumbs[0]", value = Page.initialBreadCrumb) AFTER Steps.breadcrumb_url_Copy_1.output
                                                                    output
                                                                        gettingConnectionName: _.gettingConnectionName() AFTER Steps.setStore3_Copy_1.output
                                                                            output
                                                                                if2: System.If(condition = Page.productConnectionDetails.content[0].connectionName != undefined) AFTER Steps.gettingConnectionName.output
                                                                                    true
                                                                                        setStore19: UIEngine.SetStore(path = "Page.isProductMapped", value = true) AFTER Steps.if2.true
                                                                                    false
                                                                                        setStore19_Copy_1: UIEngine.SetStore(path = "Page.isProductMapped", value = false) AFTER Steps.if2.false