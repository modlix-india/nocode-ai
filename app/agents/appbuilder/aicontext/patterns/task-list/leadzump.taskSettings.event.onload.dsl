FUNCTION onload
    LOGIC
        toggle: UIEngine.SetStore(path = "Page.isHelpVisible", value = `false`)
            output
                customTasksRep: UIEngine.SetStore(path = "Page.customTasks", value = [{
    "image": "api/files/static/file/SYSTEM/Leadzump/taskSettings/Group<PHONE>.svg",
    "name": "WhatsApp"
}, {
    "image": "api/files/static/file/SYSTEM/Leadzump/taskSettings/Group<PHONE>.svg",
    "name": "Visit"
}]) AFTER Steps.toggle.output
                    output
                        standardTasksRep: UIEngine.SetStore(path = "Page.standardTasks", value = [{
    "name": "Call"
}, {
    "name": "Call Back"
}, {
    "name": "Whatsapp"
}]) AFTER Steps.customTasksRep.output
                            output
                                dummyData: UIEngine.SetStore(path = "Page.helpData", value = [{
    "question": "What are Task configuration ?",
    "icon": "mi material-icons mio-fiber_manual_record"
}, {
    "question": "Configuration refers to the process of customizing the fields associated ?",
    "icon": "mi material-icons mio-fiber_manual_record"
}, {
    "question": "What makes a real estate website stand out?",
    "icon": "mi material-icons mio-fiber_manual_record"
}]) AFTER Steps.standardTasksRep.output