FUNCTION onLoad
    LOGIC
        setStore1: UIEngine.SetStore(path = "Page.Data", value = [])
        setStore: UIEngine.SetStore(path = "Page.leadFormDummyData", value = [{
    "existFormName": "Cityville gardern layout project",
    "DateTime": "August 2nd 2024, 11:00:38 AM"
}, {
    "existFormName": "Cityville gardern layout project",
    "DateTime": "August 2nd 2024, 11:00:38 AM"
}, {
    "existFormName": "Cityville gardern layout project",
    "DateTime": "August 2nd 2024, 11:00:38 AM"
}, {
    "existFormName": "Cityville gardern layout project",
    "DateTime": "August 2nd 2024, 11:00:38 AM"
}, {
    "existFormName": "Cityville gardern layout project",
    "DateTime": "August 2nd 2024, 11:00:38 AM"
}, {
    "existFormName": "Cityville gardern layout project",
    "DateTime": "August 2nd 2024, 11:00:38 AM"
}, {
    "existFormName": "Cityville gardern layout project",
    "DateTime": "August 2nd 2024, 11:00:38 AM"
}])
        setStore5: UIEngine.SetStore(path = "Page.introductionlistOfValues", value = [])
        setStore4: UIEngine.SetStore(path = "Page.previewLabels", value = [])
        setStore6: UIEngine.SetStore(path = "Page.leadForm.questions", value = [])
        setStore3: UIEngine.SetStore(path = "Page.formButtons", value = [{
    "label": "Full Name",
    "placeholder": "Enter your name",
    "questions": {
        "type": "FULL_NAME",
        "key": "q1"
    }
}, {
    "label": "First name",
    "placeholder": "Enter your First name",
    "questions": {
        "type": "FIRST_NAME",
        "key": "q2"
    }
}, {
    "label": "Last name",
    "placeholder": "Enter your Last name",
    "questions": {
        "type": "LAST_NAME",
        "key": "q3"
    }
}, {
    "label": "Email Id",
    "placeholder": "Enter your Email id",
    "questions": {
        "type": "EMAIL",
        "key": "q4"
    }
}, {
    "label": "Phone number",
    "placeholder": "Enter your Phone number",
    "questions": {
        "type": "PHONE",
        "key": "q5"
    }
}])
        setStore7: UIEngine.SetStore(path = "Page.formtypeInRadioButton", value = [{
    "name": "More volume",
    "value": false
}, {
    "name": "Higher intent",
    "value": true
}])
            output
                setStore8: UIEngine.SetStore(path = "Page.leadForm.is_optimized_for_quality", value = false) AFTER Steps.setStore7.output
        setStore7_Copy_1: UIEngine.SetStore(path = "Page.introductionInRadioButton", value = [{
    "name": "Paragraph",
    "value": "PARAGRAPH_STYLE"
}, {
    "name": "List",
    "value": "LIST_STYLE"
}])
            output
                setStore8_Copy_2: UIEngine.SetStore(path = "Page.leadForm.context_card.style", value = "PARAGRAPH_STYLE") AFTER Steps.setStore7_Copy_1.output
        setStore7_Copy_1_Copy_1: UIEngine.SetStore(path = "Page.completionInRadioButton", value = [{
    "name": "Go to website",
    "value": "VIEW_WEBSITE"
}, {
    "name": "Download",
    "value": "DOWNLOAD"
}, {
    "name": "Call Business",
    "value": "CALL_BUSINESS"
}])
            output
                setStore8_Copy_1: UIEngine.SetStore(path = "Page.leadForm.thank_you_page.button_type", value = "VIEW_WEBSITE") AFTER Steps.setStore7_Copy_1_Copy_1.output
        setStore9: UIEngine.SetStore(path = "Page.listOfText", value = [])
        fetchData1: UIEngine.FetchData(url = `'https://graph.facebook.com/v17.0/<PHONE>/picture?redirect=false&access_token=EAAPpL9bkipABO7jVg3Hqt953ACgukmm3Yidm8SZCm8ZBMWSWFMLtbq8vAxbAhLfCqOzDZCE3O2Qonzr9g80m9UKZA1wu0vVbZBcehohZCQUwBPL3nV06L2E8HYPBBFCStFIrGyZCozQF96ZBua60KXrZCZAsfnRUfxH3jZBuZCvBxlZCDHDyjywhKBPfs8IwlWlAFvzUgrTSDFWwA'`)
            output
                setStore_Copy_1: UIEngine.SetStore(path = "Page.pageLogo", value = Steps.fetchData1.output.data.data.url)
        fetchData: UIEngine.FetchData(url = `'https://graph.facebook.com/v17.0/<PHONE>?fields=id,name&access_token=EAAPpL9bkipABO7jVg3Hqt953ACgukmm3Yidm8SZCm8ZBMWSWFMLtbq8vAxbAhLfCqOzDZCE3O2Qonzr9g80m9UKZA1wu0vVbZBcehohZCQUwBPL3nV06L2E8HYPBBFCStFIrGyZCozQF96ZBua60KXrZCZAsfnRUfxH3jZBuZCvBxlZCDHDyjywhKBPfs8IwlWlAFvzUgrTSDFWwA&='`)
            output
                setStore_Copy_1_Copy_1: UIEngine.SetStore(path = "Page.pageName", value = Steps.fetchData.output.data.name)
        setStore_Copy_2: UIEngine.SetStore(path = "Page.form", value = false)
        setStore1_Copy_1: UIEngine.SetStore(path = "Page.newForm", value = false)