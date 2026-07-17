FUNCTION saveBpConfigurationDetails
    LOGIC
        saveBpConfiguration: leadzump.saveBpConfiguration(bpConfiguration = Page.bpConfiguration)
            output
                getBpConfigurationDetails: _.getBpConfigurationDetails() AFTER Steps.saveBpConfiguration.output
                if: System.If(condition = Steps.saveBpConfiguration.output.bpConfiguration)
                    true
                        if1: System.If(condition = `Page.activeTab = "Deals"`) AFTER Steps.if.true
                            false
                                setStore: UIEngine.SetStore(path = "Page.status", value = {
    "message": "Configuration saved successfully.",
    "type": "success"
}) AFTER Steps.if1.false
                                    output
                                        showStatus: _.showStatus() AFTER Steps.setStore.output