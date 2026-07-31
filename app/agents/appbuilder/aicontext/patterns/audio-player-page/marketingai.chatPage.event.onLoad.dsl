FUNCTION onLoad
    LOGIC
        setStore2_Copy_1: UIEngine.SetStore(value = [], path = "Page.responseData")
            output
                setStore: UIEngine.SetStore(path = "Page.userData", value = []) AFTER Steps.setStore2_Copy_1.output
                    output
                        postRequest: CoreServices.REST.PostRequest(url = "/api/ds/chat/start-session", appCode = "marketingai", connectionName = "TESTCHATAI") AFTER Steps.setStore.output
                            output
                                setStore1: UIEngine.SetStore(value = Steps.postRequest.output.data, path = "Page.message")
                                    output
                                        setStore2: UIEngine.SetStore(value = Page.message.session_id, path = "Page.sessionId") AFTER Steps.setStore1.output
                                            output
                                                setStore3: UIEngine.SetStore(path = "Page.collectedFields", value = [{
    "text": "Business Name",
    "status": false,
    "field": "businessName",
    "icon": "fa fa-solid fa-message"
}, {
    "text": "Website URL",
    "status": false,
    "field": "websiteURL",
    "icon": "fa fa-solid fa-link"
}, {
    "text": "Budget Amount",
    "status": false,
    "field": "budget",
    "icon": "fa fa-solid fa-coins"
}, {
    "text": "Campaign Duration",
    "status": false,
    "field": "durationDays",
    "icon": "fa fa-regular fa-clock"
}, {
    "text": "Select account",
    "status": false,
    "field": "loginCustomerId",
    "icon": "fa fa-solid fa-user"
}]) AFTER Steps.setStore2.output
                                                    output
                                                        setStore_Copy_1: UIEngine.SetStore(path = "Page.scrapingStatus", value = false) AFTER Steps.setStore3.output /* setting_scraping_status_to_true
 */
                                                            output
                                                                setStore4: UIEngine.SetStore(path = "Page.generatingSummeryStatus", value = false) AFTER Steps.setStore_Copy_1.output
                                                                    output
                                                                        fetchingCustomersAccountsMCC: _.fetchingCustomersAccountsMCC() AFTER Steps.setStore4.output