FUNCTION onLoad
    LOGIC
        isVisible: UIEngine.SetStore(path = "Page.isHelpVisible", value = not Page.isHelpVisible)
        dummyDataConfigField: UIEngine.SetStore(path = "Page.dummyDataConfigField", value = [{
    "question": "Can I edit or delete a custom field after creating it?"
}, {
    "question": "Are there any limitations on the number of custom fields I can create?"
}, {
    "question": "Can I rearrange the order of custom fields in the deal form?"
}, {
    "question": "How can I ensure my team fills in important fields?"
}])
        addedCustomFields: UIEngine.SetStore(path = "Page.addedCustomFields", value = [])
        activeFieldsDummyData: UIEngine.SetStore(value = [{
    "name": "Full name"
}, {
    "name": "Phone number"
}, {
    "name": "Whatsapp number"
}, {
    "name": "Email address"
}, {
    "name": "Source"
}, {
    "name": "Sub source"
}, {
    "name": "Status"
}, {
    "name": "Sub status"
}, {
    "name": "Last interaction"
}, {
    "name": "Deal owner"
}], path = "Page.activeFieldsDummyData")
        EmptyAddCustumField: UIEngine.SetStore(path = "Page.addedCustomFieldsNew", value = [])
        profileDummyData: UIEngine.SetStore(value = [{
    "name": "Full name",
    "value": "Neetu Saxsena"
}, {
    "name": "Opportunity ID",
    "value": "<PHONE>"
}, {
    "name": "Email address",
    "value": "<EMAIL>"
}, {
    "name": "Phone number",
    "value": "+<PHONE>"
}, {
    "name": "Whatsapp number",
    "value": "+<PHONE>"
}, {
    "name": "Source",
    "value": "Social media"
}, {
    "name": "Sub-source",
    "value": "Instagram"
}, {
    "name": "Status",
    "value": "Instagram"
}, {
    "name": "Sub-status",
    "value": "Instagram"
}, {
    "name": "Last interaction",
    "value": "17th July, 2024"
}, {
    "name": "Assigned user",
    "value": "Siddharth raj"
}], path = "Page.profileDummyData")
        previewOneTab: _.previewOneTab()
        dummyCustomFields: UIEngine.SetStore(value = [{
    "image": "api/files/static/file/SYSTEM/leadzump/dealSettings/profile.svg",
    "name": "Opportunity owner",
    "isSelected": false,
    "type": "Text input"
}, {
    "image": "api/files/static/file/SYSTEM/leadzump/dealSettings/mail.svg",
    "name": "Work email",
    "isSelected": false,
    "type": "Email"
}, {
    "image": "api/files/static/file/SYSTEM/leadzump/dealSettings/phoneNumber.svg",
    "name": "Alternate phone number",
    "isSelected": false,
    "type": "Phone number"
}], path = "Page.dummyCustomFields")