
class ParseError(Exception):
    pass



def pascal_to_snake_case(name) -> str:
    """
    convert case
    HelloWorld -> hello_world
    :return:
    """
    snake_name = []
    for i, ch in enumerate(name):
        if i == 0:
            snake_name.append(ch.lower())
        elif ch.isupper():
            snake_name.append('_' + ch.lower())
        else:
            snake_name.append(ch)

    return ''.join(snake_name)

