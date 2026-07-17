FUNCTION navigateToDeal
    LOGIC
        split: System.String.Split(string = Parent.message, searchString = `" "`)
            output
                setStore1: UIEngine.SetStore(path = "Page.dealCode", value = Steps.split.output.result[{{Steps.split.output.result.length -1}}])
                    output
                        navigate: UIEngine.Navigate(linkPath = `'/dealProfile/{{Page.dealCode}}'`, target = "_blank") AFTER Steps.setStore1.output