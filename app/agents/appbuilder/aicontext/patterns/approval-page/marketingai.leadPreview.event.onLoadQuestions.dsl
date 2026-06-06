FUNCTION onLoadQuestions
    LOGIC
        setStore7: UIEngine.SetStore(path = "Page.backgroundImageRadio", value = [{
    "name": "Use existing Ad image ",
    "value": false
}, {
    "name": "Upload a new image",
    "value": true
}])
            output
                setStore: UIEngine.SetStore(path = "Page.questionsArray", value = [{
    "question_no": 1,
    "name": "Multiple choice",
    "label": "",
    "type": "CUSTOM",
    "answers": [
        {
            "key": "",
            "value": ""
        },
        {
            "key": "",
            "value": ""
        }
    ],
    "is_optional": false
}]) AFTER Steps.setStore7.output
                    output
                        setStore1: UIEngine.SetStore(path = "Page.FilledMultipleQuestions", value = []) AFTER Steps.setStore.output
                            output
                                setStore2: UIEngine.SetStore(path = "Page.shortAnswerArray", value = []) AFTER Steps.setStore1.output
                                    output
                                        setStore3: UIEngine.SetStore(path = "Page.questionNo", value = 1) AFTER Steps.setStore2.output
                                            output
                                                setStore4: UIEngine.SetStore(path = "Page.historyData", value = []) AFTER Steps.setStore3.output
                                                    output
                                                        setStore5: UIEngine.SetStore(path = "Page.leadform", value = {
    "context_card": {
        "cover_photo_id": ""
    }
}) AFTER Steps.setStore4.output
                                                            output
                                                                setStore6: UIEngine.SetStore(path = "Page.buttons", value = [{
    "key": "btn1",
    "value": "v1"
}, {
    "key": "btn2",
    "value": "v2"
}, {
    "key": "btn3",
    "value": "v3"
}, {
    "key": "btn4",
    "value": "v4"
}, {
    "key": "btn5",
    "value": "v5"
}]) AFTER Steps.setStore5.output