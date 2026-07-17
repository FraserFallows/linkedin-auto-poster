# Automated LinkedIn Poster

Automatically posts to my LinkedIn profile on a schedule.

- **Monday** - a rotating dev tip or motivational snippet (Unity, C#, XR, web dev). Always posts.
- **Friday** - a summary of that week's commits to [Vargr Viking](https://vargrviking.co.uk), written by Claude. Only posts if the week's work was actually substantial.

Every post is labelled as automated and links back to this repo.

## How it works

`scripts/post.py` picks the content and publishes via LinkedIn's Posts API, run on a schedule by [GitHub Actions](.github/workflows). Friday's posts are summarised by Claude.

## Links

- [LinkedIn Profile](https://www.linkedin.com/in/fraser-fallows/)
