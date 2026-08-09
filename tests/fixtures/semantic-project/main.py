class Greeter:
    def greet(self, name: str) -> str:
        return f"Hello, {name}"


greeter = Greeter()
message = greeter.greet("world")
print(message)
